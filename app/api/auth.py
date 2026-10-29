import bcrypt
import re
from uuid import UUID
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from app.core.runtime import login_guard, image_storage_dir
from app.core.rate_limit import client_ip
from app.core.normalizacion import _normalizar_whatsapp, _email_valido
from app.core.security import verificar_jwt
from app.db.compat import connect, dict_row
from app.db.session import connection_kwargs
from app.schemas.auth import (LoginPayload, RegistroPayload, VerificarRegistroPayload,
    CambiarContrasenaPayload, EstablecerContrasenaPayload, FotoPerfilPayload)
from app.services.autenticacion import _usuario_response
from app.services.perfil_foto import comprimir_foto, guardar_foto
from app.services.whatsapp.registro import solicitar_validacion, estado_validacion, crear_registro

router = APIRouter()


@router.post("/api/auth/login")
def login(payload: LoginPayload, request: Request):
    credencial = payload.credencial.strip()
    password = payload.password.strip()
    ip = client_ip(request.scope)
    if not credencial or not password:
        login_guard.failed(ip)
        raise HTTPException(status_code=401, detail="Completa tus credenciales")

    with connect(**connection_kwargs(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.id, u.apodo, u.nombres, u.correo, u.whatsapp, u.hash_contrasena,
                       u.forzar_cambio_contrasena
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
        login_guard.failed(ip)
        raise HTTPException(status_code=401, detail="Credenciales incorrectas")
    hash_contrasena = str(row["hash_contrasena"] or "").strip()
    if len(password.encode("utf-8")) > 72 or not bcrypt.checkpw(password.encode("utf-8"), hash_contrasena.encode("utf-8")):
        login_guard.failed(ip)
        raise HTTPException(status_code=401, detail="Credenciales incorrectas")
    login_guard.succeeded(ip)
    return _usuario_response(row)



@router.post("/api/auth/registro/solicitud")
def solicitud_registro(payload: RegistroPayload, request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    apodo = payload.apodo.strip()
    nombres = payload.nombres.strip()
    whatsapp = _normalizar_whatsapp(payload.whatsapp)
    correo = payload.correo.strip()
    if not apodo or not whatsapp or not payload.password.strip():
        raise HTTPException(status_code=400, detail="Usuario, WhatsApp y contraseña son obligatorios")
    if correo and not _email_valido(correo):
        raise HTTPException(status_code=400, detail="Correo invalido")
    return solicitar_validacion(connection_kwargs(), {"apodo": apodo, "whatsapp": whatsapp,
        "password": payload.password.strip(), "nombres": nombres, "correo": correo}, login_guard, client_ip(request.scope))



@router.get("/api/auth/registro/estado/{verificacion_id}")
def estado_registro(verificacion_id: UUID, request: Request, response: Response,
                    authorization: str = Header(default="", max_length=128)):
    match = re.fullmatch(r"Bearer ([A-Za-z0-9_-]{43})", authorization)
    if not match:
        raise HTTPException(401, "Solicitud no autorizada")
    response.headers["Cache-Control"] = "no-store"
    return estado_validacion(connection_kwargs(), str(verificacion_id), match[1], login_guard, client_ip(request.scope))



@router.post("/api/auth/registro")
def registrar(payload: VerificarRegistroPayload, request: Request):
    row = crear_registro(connection_kwargs(), str(payload.verificacionId), payload.verificacionToken,
                             login_guard, client_ip(request.scope))
    return _usuario_response(row)



@router.get("/api/auth/me")
def me(usuario=Depends(verificar_jwt)):
    with connect(**connection_kwargs(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.id, u.apodo, u.nombres, u.correo, u.whatsapp, '' AS hash_contrasena,
                       u.forzar_cambio_contrasena
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



@router.post("/api/auth/cambiar-contrasena")
def cambiar_contrasena(payload: CambiarContrasenaPayload, usuario=Depends(verificar_jwt)):
    contrasena_actual = payload.contrasena_actual.strip()
    contrasena_nueva = payload.contrasena_nueva.strip()
    if not contrasena_actual:
        raise HTTPException(status_code=400, detail="Ingresa tu contraseña actual")
    if not contrasena_nueva:
        raise HTTPException(status_code=400, detail="Ingresa tu nueva contraseña")
    with connect(**connection_kwargs(), row_factory=dict_row) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT hash_contrasena
                    FROM seguridad.usuario
                    WHERE id = %s
                      AND estado = 'activo'
                    LIMIT 1
                    """,
                    (usuario["sub"],),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=401, detail="Usuario no encontrado")
                hash_actual = str(row["hash_contrasena"] or "").strip()
                if not bcrypt.checkpw(contrasena_actual.encode("utf-8"), hash_actual.encode("utf-8")):
                    raise HTTPException(status_code=401, detail="Contraseña actual incorrecta")
                nuevo_hash = bcrypt.hashpw(contrasena_nueva.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
                cur.execute(
                    """
                    UPDATE seguridad.usuario
                    SET hash_contrasena = %s,
                        forzar_cambio_contrasena = false
                    WHERE id = %s
                    """,
                    (nuevo_hash, usuario["sub"]),
                )
    return {"ok": True}



@router.post("/api/auth/establecer-contrasena")
def establecer_contrasena(payload: EstablecerContrasenaPayload, usuario=Depends(verificar_jwt)):
    contrasena_nueva = payload.contrasena_nueva
    if not contrasena_nueva:
        raise HTTPException(status_code=400, detail="Ingresa tu nueva contraseña")
    if len(contrasena_nueva.encode("utf-8")) > 72:
        raise HTTPException(status_code=400, detail="La contraseña es demasiado larga")
    with connect(**connection_kwargs(), row_factory=dict_row) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT forzar_cambio_contrasena FROM seguridad.usuario WHERE id = %s AND estado = 'activo' FOR UPDATE",
                    (usuario["sub"],),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=401, detail="Usuario no encontrado")
                if not row["forzar_cambio_contrasena"]:
                    raise HTTPException(status_code=409, detail="El cambio obligatorio ya fue completado")
                nuevo_hash = bcrypt.hashpw(contrasena_nueva.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
                cur.execute(
                    "UPDATE seguridad.usuario SET hash_contrasena = %s, forzar_cambio_contrasena = false WHERE id = %s",
                    (nuevo_hash, usuario["sub"]),
                )
    return {"ok": True}



@router.put("/api/auth/foto-perfil")
def subir_foto_perfil(payload: FotoPerfilPayload, usuario=Depends(verificar_jwt)):
    foto = comprimir_foto(payload.contenidoBase64)
    ruta = guardar_foto(connection_kwargs(), image_storage_dir, usuario["sub"], foto)
    return {"fotoUrl": ruta}



@router.delete("/api/auth/foto-perfil")
def quitar_foto_perfil(usuario=Depends(verificar_jwt)):
    guardar_foto(connection_kwargs(), image_storage_dir, usuario["sub"], None)
    return {"fotoUrl": ""}
