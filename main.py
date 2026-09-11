from datetime import datetime, timedelta, timezone
from os import getenv
from pathlib import Path
import asyncio
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
                JOIN seguridad.categoria_rol cr ON cr.usuario_id = u.id
                WHERE (lower(u.correo::text) = lower(%s)
                       OR lower(u.apodo::text) = lower(%s)
                       OR u.numero_documento = %s)
                  AND u.estado = 'activo'
                  AND cr.rol = 'cliente'
                  AND COALESCE(cr.activo, true) = true
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
                JOIN seguridad.categoria_rol cr ON cr.usuario_id = u.id
                WHERE u.id = %s
                  AND u.estado = 'activo'
                  AND cr.rol = 'cliente'
                  AND COALESCE(cr.activo, true) = true
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
                           SELECT f.ruta
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

