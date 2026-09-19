from datetime import datetime, timedelta, timezone
from os import getenv
from pathlib import Path
import asyncio
import base64
import binascii
import json
import re
from typing import Any
from uuid import uuid4

import bcrypt
import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import get_settings
from db_compat import dict_row, connect

JWT_FALLBACK_SECRET = "bitstroid-local-dev-secret-32-bytes-minimo"

app = FastAPI(title="Bitstroid API", docs_url=None, redoc_url=None, openapi_url=None)
settings = get_settings()

cors_origins = [
    origin.strip()
    for origin in getenv(
        "CORS_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000,http://localhost:3001,http://127.0.0.1:3001,http://localhost:3002,http://127.0.0.1:3002,http://localhost:3003,http://127.0.0.1:3003",
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def cachear_imagenes(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/uploads/") and response.status_code == 200:
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response

image_storage_dir = Path(getenv("IMAGE_STORAGE_DIR") or getenv("UPLOAD_DIR") or "/app/uploads")
image_storage_dir.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(image_storage_dir)), name="uploads")


class LoginPayload(BaseModel):
    credencial: str
    password: str


class RegistroPayload(BaseModel):
    apodo: str
    whatsapp: str
    password: str = Field(min_length=6, max_length=120)
    nombres: str = ""
    correo: str = ""


class InteraccionPublicacionPayload(BaseModel):
    publicacionId: str
    categoria: str = ""
    evento: str = "click"


class ConfirmacionPagoPayload(BaseModel):
    total: float = Field(ge=0)
    items: list[dict[str, Any]] = Field(default_factory=list)
    comprobanteNombre: str
    comprobanteMimeType: str
    comprobanteContenidoBase64: str
    invitadoNombre: str = ""
    invitadoWhatsapp: str = ""


class SolicitudEnvioPayload(BaseModel):
    tipo: str
    destinatarioId: str = ""


class DestinatarioEnvioPayload(BaseModel):
    nombreCompleto: str
    dni: str
    direccion: str
    referencia: str = ""
    provincia: str
    distrito: str
    contacto: str
    predeterminado: bool = False


def _connection_kwargs() -> dict[str, Any]:
    db = settings.database
    return {
        "host": db.host,
        "port": db.port,
        "dbname": db.database,
        "user": db.username,
        "password": db.password,
        "connect_timeout": 8,
    }


def _normalizar_whatsapp(valor: str) -> str:
    digitos = re.sub(r"\D+", "", valor or "")
    if len(digitos) == 9:
        digitos = f"51{digitos}"
    if not re.match(r"^[0-9]{8,15}$", digitos):
        raise HTTPException(status_code=400, detail="WhatsApp invalido")
    return digitos


def _email_valido(valor: str) -> bool:
    return bool(re.match(r"^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", valor, flags=re.I))


def _crear_token(usuario: dict[str, Any]) -> str:
    secret = getenv("JWT_SECRET") or getenv("SECRET_KEY") or JWT_FALLBACK_SECRET
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(usuario["id"]),
        "apodo": usuario["apodo"],
        "rol": "cliente",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=4)).timestamp()),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def _usuario_response(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "apodo": row["apodo"] or "",
        "nombres": row["nombres"] or "",
        "correo": row["correo"] or "",
        "whatsapp": row["whatsapp"] or "",
        "rol": "cliente",
        "accessToken": _crear_token(row),
    }


def verificar_jwt(authorization: str = Header(default="")) -> dict[str, Any]:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token requerido")
    token = authorization.removeprefix("Bearer ").strip()
    secret = getenv("JWT_SECRET") or getenv("SECRET_KEY") or JWT_FALLBACK_SECRET
    try:
        return jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=401, detail="Sesion expirada") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="Token invalido") from exc


def _jwt_opcional(authorization: str = Header(default="")) -> dict[str, Any] | None:
    if not authorization.startswith("Bearer "):
        return None
    try:
        return verificar_jwt(authorization)
    except HTTPException:
        return None


def _ensure_cliente_interacciones() -> None:
    with connect(**_connection_kwargs(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS cliente")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS cliente.publicacion_interaccion (
                    id uuid PRIMARY KEY,
                    usuario_id uuid NOT NULL REFERENCES seguridad.usuario(id) ON DELETE CASCADE,
                    publicacion_id uuid NOT NULL,
                    categoria varchar(160) NOT NULL DEFAULT '',
                    evento varchar(40) NOT NULL DEFAULT 'click',
                    creado_en timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS cliente_publicacion_interaccion_usuario_idx ON cliente.publicacion_interaccion(usuario_id, creado_en DESC)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS cliente_publicacion_interaccion_categoria_idx ON cliente.publicacion_interaccion(usuario_id, categoria)"
            )


def _ensure_pago_tables() -> None:
    with connect(**_connection_kwargs(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS publicacion")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS publicacion.configuracion_pago (
                    id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
                    contacto_celular varchar(30) NOT NULL DEFAULT '',
                    qr_ruta text NOT NULL DEFAULT '',
                    actualizado_por_usuario_id uuid REFERENCES seguridad.usuario(id) ON DELETE SET NULL,
                    actualizado_en timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute("INSERT INTO publicacion.configuracion_pago (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS publicacion.solicitud_pago (
                    id uuid PRIMARY KEY,
                    usuario_id uuid REFERENCES seguridad.usuario(id) ON DELETE SET NULL,
                    total numeric(14, 2) NOT NULL,
                    items jsonb NOT NULL DEFAULT '[]'::jsonb,
                    comprobante_ruta text NOT NULL,
                    estado varchar(20) NOT NULL DEFAULT 'pendiente',
                    invitado_nombre varchar(160) NOT NULL DEFAULT '',
                    invitado_whatsapp varchar(30) NOT NULL DEFAULT '',
                    validado_por_usuario_id uuid REFERENCES seguridad.usuario(id) ON DELETE SET NULL,
                    validado_en timestamptz,
                    envio_tipo varchar(30),
                    envio_solicitado_en timestamptz,
                    creado_en timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute("ALTER TABLE publicacion.solicitud_pago ADD COLUMN IF NOT EXISTS invitado_nombre varchar(160) NOT NULL DEFAULT ''")
            cur.execute("ALTER TABLE publicacion.solicitud_pago ADD COLUMN IF NOT EXISTS invitado_whatsapp varchar(30) NOT NULL DEFAULT ''")
            cur.execute("ALTER TABLE publicacion.solicitud_pago ADD COLUMN IF NOT EXISTS validado_por_usuario_id uuid REFERENCES seguridad.usuario(id) ON DELETE SET NULL")
            cur.execute("ALTER TABLE publicacion.solicitud_pago ADD COLUMN IF NOT EXISTS validado_en timestamptz")
            cur.execute("ALTER TABLE publicacion.solicitud_pago ADD COLUMN IF NOT EXISTS envio_tipo varchar(30)")
            cur.execute("ALTER TABLE publicacion.solicitud_pago ADD COLUMN IF NOT EXISTS envio_solicitado_en timestamptz")
            cur.execute("CREATE SCHEMA IF NOT EXISTS cliente")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS cliente.destinatario_envio (
                    id uuid PRIMARY KEY,
                    usuario_id uuid NOT NULL REFERENCES seguridad.usuario(id) ON DELETE CASCADE,
                    nombre_completo varchar(180) NOT NULL,
                    dni varchar(20) NOT NULL,
                    direccion text NOT NULL,
                    referencia text NOT NULL DEFAULT '',
                    provincia varchar(120) NOT NULL,
                    distrito varchar(120) NOT NULL,
                    contacto varchar(30) NOT NULL,
                    predeterminado boolean NOT NULL DEFAULT false,
                    activo boolean NOT NULL DEFAULT true,
                    creado_en timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute("ALTER TABLE publicacion.solicitud_pago ADD COLUMN IF NOT EXISTS destinatario_id uuid REFERENCES cliente.destinatario_envio(id) ON DELETE SET NULL")
            conn.commit()


@app.get("/")
def root():
    return {"ok": True, "message": "Hola Bitstroid"}


@app.post("/api/auth/login")
def login(payload: LoginPayload):
    credencial = payload.credencial.strip()
    password = payload.password.strip()
    if not credencial or not password:
        raise HTTPException(status_code=401, detail="Completa tus credenciales")

    with connect(**_connection_kwargs(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.id, u.apodo, u.nombres, u.correo, u.whatsapp, u.hash_contrasena
                FROM seguridad.usuario u
                WHERE (lower(u.correo::text) = lower(%s)
                       OR lower(u.apodo::text) = lower(%s)
                       OR u.numero_documento = %s)
                  AND u.estado = 'activo'
                LIMIT 1
                """,
                (credencial, credencial, credencial),
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Credenciales incorrectas")
    hash_contrasena = str(row["hash_contrasena"] or "").strip()
    if not bcrypt.checkpw(password.encode("utf-8"), hash_contrasena.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Credenciales incorrectas")
    return _usuario_response(row)


@app.post("/api/auth/registro")
def registrar(payload: RegistroPayload):
    apodo = payload.apodo.strip()
    nombres = payload.nombres.strip()
    whatsapp = _normalizar_whatsapp(payload.whatsapp)
    correo = payload.correo.strip()
    if not apodo or not whatsapp:
        raise HTTPException(status_code=400, detail="Apodo y WhatsApp son obligatorios")
    if not correo:
        correo = f"{re.sub(r'[^a-zA-Z0-9._-]+', '_', apodo).lower()}@bitstroid.local"
    if not _email_valido(correo):
        raise HTTPException(status_code=400, detail="Correo invalido")

    hash_contrasena = bcrypt.hashpw(payload.password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    usuario_id = str(uuid4())
    with connect(**_connection_kwargs(), row_factory=dict_row) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        EXISTS (SELECT 1 FROM seguridad.usuario WHERE lower(apodo::text) = lower(%s)) AS apodo_existe,
                        EXISTS (SELECT 1 FROM seguridad.usuario WHERE whatsapp = %s) AS whatsapp_existe,
                        EXISTS (SELECT 1 FROM seguridad.usuario WHERE lower(correo::text) = lower(%s)) AS correo_existe
                    """,
                    (apodo, whatsapp, correo),
                )
                existe = cur.fetchone()
                if existe["apodo_existe"]:
                    raise HTTPException(status_code=409, detail="El apodo ya existe")
                if existe["whatsapp_existe"]:
                    raise HTTPException(status_code=409, detail="El WhatsApp ya existe")
                if existe["correo_existe"]:
                    raise HTTPException(status_code=409, detail="El correo ya existe")
                cur.execute(
                    """
                    INSERT INTO seguridad.usuario (
                        id, apodo, nombres, correo, whatsapp, hash_contrasena,
                        forzar_cambio_contrasena, correo_verificado, whatsapp_verificado, estado
                    )
                    VALUES (%s, %s, NULLIF(%s, ''), %s, %s, %s, false, false, false, 'activo')
                    RETURNING id, apodo, nombres, correo, whatsapp
                    """,
                    (usuario_id, apodo, nombres, correo, whatsapp, hash_contrasena),
                )
                row = cur.fetchone()
                cur.execute(
                    """
                    INSERT INTO seguridad.categoria_rol (usuario_id, rol, activo)
                    VALUES (%s, 'cliente', true)
                    ON CONFLICT DO NOTHING
                    """,
                    (usuario_id,),
                )
                cur.execute(
                    """
                    INSERT INTO cliente.cliente (usuario_id, puntos, activo, creado_por_usuario_id)
                    VALUES (%s, 0, true, %s)
                    ON CONFLICT (usuario_id) DO UPDATE
                    SET activo = true,
                        actualizado_en = now()
                    """,
                    (usuario_id, usuario_id),
                )
    return _usuario_response(row)


@app.get("/api/auth/me")
def me(usuario=Depends(verificar_jwt)):
    with connect(**_connection_kwargs(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.id, u.apodo, u.nombres, u.correo, u.whatsapp, '' AS hash_contrasena
                FROM seguridad.usuario u
                WHERE u.id = %s
                  AND u.estado = 'activo'
                LIMIT 1
                """,
                (usuario["sub"],),
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Usuario no encontrado")
    return {k: v for k, v in _usuario_response(row).items() if k != "accessToken"}


@app.post("/api/interacciones/publicacion")
def registrar_interaccion_publicacion(payload: InteraccionPublicacionPayload, usuario=Depends(_jwt_opcional)):
    if not usuario:
        return {"ok": True, "registrado": False}
    publicacion_id = payload.publicacionId.strip()
    if not publicacion_id:
        raise HTTPException(status_code=400, detail="Publicacion requerida")
    _ensure_cliente_interacciones()
    categoria = payload.categoria.strip()[:160]
    evento = (payload.evento.strip() or "click")[:40]
    with connect(**_connection_kwargs(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cliente.publicacion_interaccion (id, usuario_id, publicacion_id, categoria, evento)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (str(uuid4()), usuario["sub"], publicacion_id, categoria, evento),
            )
    return {"ok": True, "registrado": True}


@app.get("/api/interacciones/preferencias")
def obtener_preferencias_publicacion(usuario=Depends(verificar_jwt)):
    _ensure_cliente_interacciones()
    with connect(**_connection_kwargs(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT categoria, count(*) AS total
                FROM cliente.publicacion_interaccion
                WHERE usuario_id = %s
                  AND btrim(categoria) <> ''
                  AND creado_en >= now() - interval '90 days'
                GROUP BY categoria
                ORDER BY total DESC, max(creado_en) DESC
                LIMIT 5
                """,
                (usuario["sub"],),
            )
            categorias = cur.fetchall()
            cur.execute(
                """
                SELECT publicacion_id, max(creado_en) AS ultima_visita, count(*) AS total
                FROM cliente.publicacion_interaccion
                WHERE usuario_id = %s
                GROUP BY publicacion_id
                ORDER BY ultima_visita DESC
                LIMIT 12
                """,
                (usuario["sub"],),
            )
            publicaciones = cur.fetchall()
    return {
        "categorias": [{"categoria": row["categoria"], "total": int(row["total"] or 0)} for row in categorias],
        "publicaciones": [
            {
                "publicacionId": str(row["publicacion_id"]),
                "total": int(row["total"] or 0),
                "ultimaVisita": row["ultima_visita"].isoformat() if row["ultima_visita"] else "",
            }
            for row in publicaciones
        ],
    }


@app.get("/api/publico/configuracion-pago")
def obtener_configuracion_pago_publica():
    _ensure_pago_tables()
    with connect(**_connection_kwargs(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT contacto_celular, qr_ruta FROM publicacion.configuracion_pago WHERE id = 1")
            row = cur.fetchone() or {}
    return {"contactoCelular": row.get("contacto_celular") or "", "qrRuta": row.get("qr_ruta") or ""}


@app.post("/api/publico/pagos/confirmar")
def confirmar_pago(payload: ConfirmacionPagoPayload, usuario=Depends(_jwt_opcional)):
    _ensure_pago_tables()
    if not payload.items:
        raise HTTPException(status_code=400, detail="No hay productos para confirmar")
    invitado_nombre = payload.invitadoNombre.strip()
    invitado_whatsapp = payload.invitadoWhatsapp.strip()
    if not usuario:
        if not invitado_nombre:
            raise HTTPException(status_code=400, detail="Ingresa tu nombre para continuar como invitado")
        invitado_whatsapp = _normalizar_whatsapp(invitado_whatsapp)
    if not payload.comprobanteMimeType.startswith("image/"):
        raise HTTPException(status_code=400, detail="El comprobante debe ser una imagen")
    try:
        contenido = payload.comprobanteContenidoBase64.split(",", 1)[-1]
        data = base64.b64decode(contenido, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Comprobante inválido") from exc
    if not data or len(data) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="El comprobante debe pesar menos de 5 MB")

    extension = Path(payload.comprobanteNombre).suffix.lower()
    if extension not in {".jpg", ".jpeg", ".png", ".webp"}:
        extension = ".jpg"
    solicitud_id = str(uuid4())
    carpeta = image_storage_dir / "pagos" / "comprobantes"
    carpeta.mkdir(parents=True, exist_ok=True)
    nombre = f"{solicitud_id}{extension}"
    (carpeta / nombre).write_bytes(data)
    ruta = f"/uploads/pagos/comprobantes/{nombre}"

    with connect(**_connection_kwargs(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO publicacion.solicitud_pago (
                    id, usuario_id, total, items, comprobante_ruta, invitado_nombre, invitado_whatsapp
                ) VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)
                """,
                (solicitud_id, usuario.get("sub") if usuario else None, payload.total, json.dumps(payload.items, ensure_ascii=False), ruta, invitado_nombre, invitado_whatsapp),
            )
            conn.commit()
    return {"ok": True, "solicitudId": solicitud_id, "estado": "pendiente"}


@app.get("/api/cliente/almacen")
def listar_almacen_cliente(usuario=Depends(verificar_jwt)):
    _ensure_pago_tables()
    with connect(**_connection_kwargs(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, total, items, creado_en, validado_en, envio_tipo, envio_solicitado_en, destinatario_id
                FROM publicacion.solicitud_pago
                WHERE usuario_id = %s AND estado = 'validado'
                ORDER BY validado_en DESC NULLS LAST, creado_en DESC
                """,
                (usuario["sub"],),
            )
            rows = cur.fetchall()
    return [{
        "id": str(row["id"]), "total": float(row["total"] or 0), "items": row["items"] or [],
        "creadoEn": row["creado_en"].isoformat(),
        "validadoEn": row["validado_en"].isoformat() if row["validado_en"] else None,
        "envioTipo": row["envio_tipo"],
        "destinatarioId": str(row["destinatario_id"]) if row["destinatario_id"] else None,
        "envioSolicitadoEn": row["envio_solicitado_en"].isoformat() if row["envio_solicitado_en"] else None,
    } for row in rows]


@app.get("/api/cliente/destinatarios")
def listar_destinatarios_cliente(usuario=Depends(verificar_jwt)):
    _ensure_pago_tables()
    with connect(**_connection_kwargs(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, nombre_completo, dni, direccion, referencia, provincia, distrito, contacto, predeterminado
                FROM cliente.destinatario_envio
                WHERE usuario_id = %s AND activo = true
                ORDER BY predeterminado DESC, creado_en DESC
                """,
                (usuario["sub"],),
            )
            rows = cur.fetchall()
    return [{
        "id": str(row["id"]), "nombreCompleto": row["nombre_completo"], "dni": row["dni"],
        "direccion": row["direccion"], "referencia": row["referencia"], "provincia": row["provincia"],
        "distrito": row["distrito"], "contacto": row["contacto"], "predeterminado": row["predeterminado"],
    } for row in rows]


@app.post("/api/cliente/destinatarios")
def crear_destinatario_cliente(payload: DestinatarioEnvioPayload, usuario=Depends(verificar_jwt)):
    _ensure_pago_tables()
    nombre = payload.nombreCompleto.strip()
    dni = re.sub(r"\D+", "", payload.dni)
    direccion = payload.direccion.strip()
    provincia = payload.provincia.strip()
    distrito = payload.distrito.strip()
    contacto = _normalizar_whatsapp(payload.contacto)
    if not nombre or not direccion or not provincia or not distrito or not 8 <= len(dni) <= 12:
        raise HTTPException(status_code=400, detail="Completa correctamente los datos del destinatario")
    destinatario_id = str(uuid4())
    with connect(**_connection_kwargs(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM cliente.destinatario_envio WHERE usuario_id = %s AND activo = true LIMIT 1", (usuario["sub"],))
            predeterminado = payload.predeterminado or not bool(cur.fetchone())
            if predeterminado:
                cur.execute("UPDATE cliente.destinatario_envio SET predeterminado = false WHERE usuario_id = %s", (usuario["sub"],))
            cur.execute(
                """
                INSERT INTO cliente.destinatario_envio (
                    id, usuario_id, nombre_completo, dni, direccion, referencia, provincia, distrito, contacto, predeterminado
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (destinatario_id, usuario["sub"], nombre, dni, direccion, payload.referencia.strip(), provincia, distrito, contacto, predeterminado),
            )
            conn.commit()
    return {"ok": True, "id": destinatario_id, "predeterminado": predeterminado}


@app.post("/api/cliente/almacen/{pago_id}/solicitar-envio")
def solicitar_envio_almacen(pago_id: str, payload: SolicitudEnvioPayload, usuario=Depends(verificar_jwt)):
    _ensure_pago_tables()
    if payload.tipo not in {"contra_entrega", "courrier"}:
        raise HTTPException(status_code=400, detail="Tipo de envío inválido")
    with connect(**_connection_kwargs(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            destinatario_id = None
            if payload.tipo == "courrier":
                if not payload.destinatarioId:
                    raise HTTPException(status_code=400, detail="Selecciona un destinatario")
                cur.execute("SELECT id FROM cliente.destinatario_envio WHERE id = %s AND usuario_id = %s AND activo = true", (payload.destinatarioId, usuario["sub"]))
                destinatario = cur.fetchone()
                if not destinatario:
                    raise HTTPException(status_code=404, detail="Destinatario no encontrado")
                destinatario_id = destinatario["id"]
            cur.execute(
                """
                UPDATE publicacion.solicitud_pago
                SET envio_tipo = %s, destinatario_id = %s, envio_solicitado_en = now()
                WHERE id = %s AND usuario_id = %s AND estado = 'validado'
                RETURNING id, envio_solicitado_en
                """,
                (payload.tipo, destinatario_id, pago_id, usuario["sub"]),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Compra no encontrada")
            conn.commit()
    return {"ok": True, "envioTipo": payload.tipo, "envioSolicitadoEn": row["envio_solicitado_en"].isoformat()}


@app.get("/api/publico/catalogo")
def listar_catalogo_publico():
    with connect(**_connection_kwargs(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id AS publicacion_id,
                       p.codigo AS publicacion_codigo,
                       p.titulo,
                       p.descripcion,
                       p.creado_en AS publicacion_creado_en,
                       i.id AS item_id,
                       i.codigo AS item_codigo,
                       i.nombre AS item_nombre,
                       COALESCE(cat.nombre, '') AS categoria_nombre,
                       COALESCE(cat.abreviatura, '') AS categoria_abreviatura,
                       COALESCE(estilo.codigo, 'default_azul') AS estilo_visual_codigo,
                       COALESCE(estilo.nombre, 'Default azul') AS estilo_visual_nombre,
                       i.precio_venta_soles,
                       i.stock_disponible,
                       (
                           SELECT COALESCE(NULLIF(f.metadata->>'miniatura_ruta', ''), f.ruta)
                           FROM inventario.publicacion_foto f
                           WHERE f.publicacion_id = p.id
                           ORDER BY COALESCE((f.metadata->>'es_portada')::boolean, false) DESC,
                                    f.orden ASC,
                                    f.creado_en ASC
                           LIMIT 1
                       ) AS imagen_url,
                       COALESCE((
                           SELECT json_agg(json_build_object(
                               'ruta', fotos.ruta,
                               'orden', fotos.orden,
                               'esPortada', COALESCE((fotos.metadata->>'es_portada')::boolean, false)
                           ) ORDER BY COALESCE((fotos.metadata->>'es_portada')::boolean, false) DESC, fotos.orden ASC, fotos.creado_en ASC)
                           FROM inventario.publicacion_foto fotos
                           WHERE fotos.publicacion_id = p.id
                       ), '[]'::json) AS fotos
                FROM inventario.publicacion p
                JOIN inventario.publicacion_item pi ON pi.publicacion_id = p.id
                JOIN inventario.item i ON i.id = pi.item_id
                LEFT JOIN costeo.categoria_compra cat ON cat.id = i.categoria_id
                LEFT JOIN publicacion.categoria_estilo_visual estilo ON estilo.id = cat.estilo_visual_id
                WHERE lower(COALESCE(p.estado, '')) = 'publicado'
                  AND COALESCE(p.visible, true) = true
                  AND lower(COALESCE(i.estado, '')) = 'disponible'
                  AND COALESCE(i.stock_disponible, 0) > 0
                ORDER BY p.creado_en DESC, pi.creado_en ASC
                LIMIT 120
                """
            )
            rows = cur.fetchall()
    return [
        {
            "publicacionId": str(row["publicacion_id"]),
            "codigo": row["publicacion_codigo"] or "",
            "titulo": row["titulo"] or "",
            "descripcion": row["descripcion"] or "",
            "fechaCreacion": row["publicacion_creado_en"].isoformat() if row["publicacion_creado_en"] else "",
            "imagenUrl": row["imagen_url"] or "",
            "fotos": row.get("fotos") or [],
            "itemId": str(row["item_id"]),
            "sku": row["item_codigo"] or "",
            "nombre": row["item_nombre"] or "",
            "categoriaNombre": row["categoria_nombre"] or "",
            "categoriaAbreviatura": row["categoria_abreviatura"] or "",
            "estiloVisualCodigo": row["estilo_visual_codigo"] or "default_azul",
            "estiloVisualNombre": row["estilo_visual_nombre"] or "Default azul",
            "precio": float(row["precio_venta_soles"] or 0),
            "moneda": "PEN",
            "stockDisponible": int(row["stock_disponible"] or 0),
        }
        for row in rows
    ]


def _ultima_publicacion_publica() -> dict[str, Any] | None:
    with connect(**_connection_kwargs(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id AS publicacion_id,
                       p.codigo,
                       p.titulo,
                       p.creado_en
                FROM inventario.publicacion p
                WHERE lower(COALESCE(p.estado, '')) = 'publicado'
                  AND COALESCE(p.visible, true) = true
                  AND EXISTS (
                      SELECT 1
                      FROM inventario.publicacion_item pi
                      JOIN inventario.item i ON i.id = pi.item_id
                      WHERE pi.publicacion_id = p.id
                        AND lower(COALESCE(i.estado, '')) = 'disponible'
                        AND COALESCE(i.stock_disponible, 0) > 0
                  )
                ORDER BY p.creado_en DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
    if not row:
        return None
    return {
        "publicacionId": str(row["publicacion_id"]),
        "codigo": row["codigo"] or "",
        "titulo": row["titulo"] or "",
        "fechaCreacion": row["creado_en"].isoformat() if row["creado_en"] else "",
    }


@app.get("/api/publico/catalogo/eventos")
async def eventos_catalogo_publico(since: str = ""):
    async def stream():
        ultima_fecha = since
        while True:
            try:
                ultima = _ultima_publicacion_publica()
                fecha = ultima["fechaCreacion"] if ultima else ""
                if ultima and fecha and fecha != ultima_fecha:
                    ultima_fecha = fecha
                    payload = json.dumps(ultima, ensure_ascii=False)
                    yield f"event: nueva-publicacion\ndata: {payload}\n\n"
                else:
                    yield ": ping\n\n"
            except Exception:
                yield ": error\n\n"
            await asyncio.sleep(6)

    return StreamingResponse(stream(), media_type="text/event-stream")

