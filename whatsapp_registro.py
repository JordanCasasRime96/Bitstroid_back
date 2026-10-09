from functools import cache
from hashlib import sha256
import hmac
import json
from os import getenv
import re
import secrets
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

import bcrypt
from fastapi import HTTPException
from limits import RateLimitItemPerHour, RateLimitItemPerMinute

from db_compat import connect, dict_row


def configuracion():
    keys = ["WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_API_VERSION",
            "WHATSAPP_AUTH_TEMPLATE", "WHATSAPP_AUTH_LANGUAGE", "WHATSAPP_OTP_SECRET"]
    values = [getenv(key, "").strip() for key in keys]
    if not all(values) or len(values[-1]) < 32:
        raise HTTPException(503, "El registro por WhatsApp aún no está disponible")
    if not values[1].isdigit() or not re.fullmatch(r"v\d+\.\d+", values[2]):
        raise HTTPException(503, "El registro por WhatsApp aún no está disponible")
    return values


def hash_codigo(id_, codigo, secret):
    return hmac.new(secret.encode(), f"{id_}:{codigo}".encode(), sha256).hexdigest()


def enviar_codigo(numero, codigo, config):
    token, phone_id, version, template, language, _ = config
    payload = {"messaging_product": "whatsapp", "to": numero, "type": "template", "template": {
        "name": template, "language": {"code": language}, "components": [
            {"type": "body", "parameters": [{"type": "text", "text": codigo}]},
            {"type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": codigo}]},
        ]}}
    request = Request(f"https://graph.facebook.com/{version}/{phone_id}/messages",
                      data=json.dumps(payload).encode(), method="POST",
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=8) as response:
            data = json.loads(response.read(16384))
        if not data.get("messages", [{}])[0].get("id"):
            raise ValueError("Sin confirmación de envío")
    except (HTTPError, URLError, TimeoutError, ValueError, IndexError, OSError) as exc:
        raise HTTPException(502, "No se pudo enviar el código a WhatsApp. Intenta más tarde.") from exc


@cache
def preparar_tabla(connection_values):
    with connect(**dict(connection_values), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS cliente")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cliente.registro_whatsapp (
                    id uuid PRIMARY KEY, whatsapp text NOT NULL, datos jsonb NOT NULL,
                    codigo_hash text NOT NULL, intentos integer NOT NULL DEFAULT 0,
                    enviado boolean NOT NULL DEFAULT false,
                    creado_en timestamptz NOT NULL DEFAULT now(),
                    vence_en timestamptz NOT NULL DEFAULT (now() + interval '5 minutes')
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS registro_whatsapp_numero_idx ON cliente.registro_whatsapp(whatsapp)")


def comprobar_duplicados(cur, datos):
    cur.execute("""
        SELECT EXISTS(SELECT 1 FROM seguridad.usuario WHERE lower(apodo::text) = lower(%s)) AS usuario,
               EXISTS(SELECT 1 FROM seguridad.usuario WHERE whatsapp = %s) AS numero,
               EXISTS(SELECT 1 FROM seguridad.usuario WHERE lower(correo::text) = lower(%s)) AS correo
    """, (datos["apodo"], datos["whatsapp"], datos["correo"]))
    existe = cur.fetchone()
    if existe["usuario"]:
        raise HTTPException(409, "El nombre de usuario ya está registrado")
    if existe["numero"]:
        raise HTTPException(409, "Este WhatsApp ya está registrado")
    if existe["correo"]:
        raise HTTPException(409, "Este correo ya está registrado")


def solicitar_codigo(kwargs, datos, guard, ip):
    config = configuracion()
    guard.limit(RateLimitItemPerMinute(5, 10), "otp-envio-ip", ip)
    guard.limit(RateLimitItemPerHour(10), "otp-envio-numero", datos["whatsapp"])
    guard.limit(RateLimitItemPerHour(60), "otp-envio-global", "all")
    if len(datos["password"].encode()) > 72:
        raise HTTPException(400, "La contraseña supera el tamaño admitido")
    preparar_tabla(tuple(sorted(kwargs.items())))
    id_ = str(uuid4())
    codigo = f"{secrets.randbelow(1_000_000):06d}"
    datos = {**datos, "hash_contrasena": bcrypt.hashpw(datos["password"].encode(), bcrypt.gensalt()).decode()}
    del datos["password"]
    if not datos["correo"]:
        datos["correo"] = f"{id_}@bitstroid.local"
    with connect(**kwargs, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM cliente.registro_whatsapp WHERE vence_en < now()")
        conn.commit()
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("registro-numero:" + datos["whatsapp"],))
                comprobar_duplicados(cur, datos)
                cur.execute("SELECT 1 FROM cliente.registro_whatsapp WHERE whatsapp = %s AND creado_en > now() - interval '60 seconds'", (datos["whatsapp"],))
                if cur.fetchone():
                    raise HTTPException(429, "Espera antes de pedir otro código", headers={"Retry-After": "60"})
                cur.execute("DELETE FROM cliente.registro_whatsapp WHERE whatsapp = %s", (datos["whatsapp"],))
                cur.execute("INSERT INTO cliente.registro_whatsapp (id, whatsapp, datos, codigo_hash) VALUES (%s, %s, %s::jsonb, %s)",
                            (id_, datos["whatsapp"], json.dumps(datos), hash_codigo(id_, codigo, config[-1])))
    try:
        enviar_codigo(datos["whatsapp"], codigo, config)
    except HTTPException:
        with connect(**kwargs) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM cliente.registro_whatsapp WHERE id = %s", (id_,))
        raise
    with connect(**kwargs) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE cliente.registro_whatsapp SET enviado = true WHERE id = %s", (id_,))
    return {"verificacionId": id_, "venceEn": 300, "reenviarEn": 60}


def verificar_registro(kwargs, id_, codigo, guard, ip):
    config = configuracion()
    guard.limit(RateLimitItemPerMinute(15), "otp-validacion-ip", ip)
    preparar_tabla(tuple(sorted(kwargs.items())))
    error = None
    with connect(**kwargs, row_factory=dict_row) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT whatsapp FROM cliente.registro_whatsapp WHERE id = %s", (id_,))
                numero = cur.fetchone()
                if not numero:
                    raise HTTPException(400, "Código inválido o vencido. Solicita uno nuevo.")
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("registro-numero:" + numero["whatsapp"],))
                cur.execute("SELECT *, vence_en <= now() AS vencido FROM cliente.registro_whatsapp WHERE id = %s FOR UPDATE", (id_,))
                solicitud = cur.fetchone()
                if not solicitud or not solicitud["enviado"] or solicitud["vencido"] or solicitud["intentos"] >= 5:
                    raise HTTPException(400, "Código inválido o vencido. Solicita uno nuevo.")
                if not hmac.compare_digest(solicitud["codigo_hash"], hash_codigo(id_, codigo, config[-1])):
                    cur.execute("UPDATE cliente.registro_whatsapp SET intentos = intentos + 1 WHERE id = %s", (id_,))
                    error = HTTPException(400, "Código incorrecto" if solicitud["intentos"] < 4 else "Demasiados intentos. Solicita un código nuevo.")
                else:
                    datos = solicitud["datos"]
                    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("registro-usuario:" + datos["apodo"].lower(),))
                    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("registro-correo:" + datos["correo"].lower(),))
                    comprobar_duplicados(cur, datos)
                    usuario_id = str(uuid4())
                    cur.execute("""
                        INSERT INTO seguridad.usuario (id, apodo, nombres, correo, whatsapp, hash_contrasena,
                            forzar_cambio_contrasena, correo_verificado, whatsapp_verificado, estado)
                        VALUES (%s, %s, NULLIF(%s, ''), %s, %s, %s, false, false, true, 'activo')
                        RETURNING id, apodo, nombres, correo, whatsapp
                    """, (usuario_id, datos["apodo"], datos["nombres"], datos["correo"], datos["whatsapp"], datos["hash_contrasena"]))
                    row = cur.fetchone()
                    cur.execute("INSERT INTO seguridad.categoria_rol (usuario_id, rol, activo) VALUES (%s, 'cliente', true) ON CONFLICT DO NOTHING", (usuario_id,))
                    cur.execute("INSERT INTO cliente.cliente (usuario_id, puntos, activo, creado_por_usuario_id) VALUES (%s, 0, true, %s) ON CONFLICT (usuario_id) DO NOTHING", (usuario_id, usuario_id))
                    cur.execute("DELETE FROM cliente.registro_whatsapp WHERE id = %s", (id_,))
    # Raise only after committing the failed-attempt counter.
    if error:
        raise error
    return row
