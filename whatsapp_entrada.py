"""Register only after a signed incoming WhatsApp message proves phone ownership."""
from functools import cache
from hashlib import sha256
import hmac
from os import getenv
import re
import secrets
import json
import logging
from urllib.parse import urlencode
from uuid import uuid4

import bcrypt
from fastapi import HTTPException
from limits import RateLimitItemPerHour, RateLimitItemPerMinute

from db_compat import connect, dict_row
from whatsapp_contactos import preparar_tablas, mensajes_entrantes, guardar_contactos, vincular_usuario
from whatsapp_respuestas import preparar_tabla as preparar_respuestas, encolar


def hash_codigo(id_, codigo, secret):
    return hmac.new(secret.encode(), f"{id_}:{codigo}".encode(), sha256).hexdigest()


def comprobar_duplicados(cur, datos):
    cur.execute("""
        SELECT EXISTS(SELECT 1 FROM seguridad.usuario WHERE lower(apodo::text) = lower(%s)) AS usuario,
               EXISTS(SELECT 1 FROM seguridad.usuario WHERE whatsapp = %s) AS numero,
               EXISTS(SELECT 1 FROM seguridad.usuario WHERE lower(correo::text) = lower(%s)) AS correo
    """, (datos["apodo"], datos["whatsapp"], datos["correo"]))
    existe = cur.fetchone()
    if existe["usuario"]:
        raise HTTPException(409, "El nombre de usuario ya esta registrado")
    if existe["numero"]:
        raise HTTPException(409, "Este WhatsApp ya esta registrado")
    if existe["correo"]:
        raise HTTPException(409, "Este correo ya esta registrado")


def configuracion():
    secret = getenv("WHATSAPP_OTP_SECRET", "").strip()
    app_secret = getenv("WHATSAPP_APP_SECRET", "").strip()
    verify_token = getenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "").strip()
    phone_id = getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    numero = getenv("WHATSAPP_BUSINESS_NUMBER", "51926839501").strip()
    if (len(secret) < 32 or not app_secret or len(verify_token) < 32
            or not phone_id.isdigit() or not re.fullmatch(r"[1-9]\d{7,14}", numero)):
        raise HTTPException(503, "La validacion por WhatsApp aun no esta disponible")
    return secret, app_secret, verify_token, phone_id, numero


def verificar_firma(body, signature, app_secret):
    expected = "sha256=" + hmac.new(app_secret.encode(), body, sha256).hexdigest()
    if not signature or not re.fullmatch(r"sha256=[0-9a-f]{64}", signature) or not hmac.compare_digest(expected, signature):
        raise HTTPException(401, "Firma de webhook invalida")


def verificar_webhook(mode, token, challenge):
    expected = configuracion()[2]
    if mode != "subscribe" or not token or not hmac.compare_digest(token.encode(), expected.encode()) or not challenge:
        raise HTTPException(403, "Verificacion de webhook invalida")
    return challenge


@cache
def preparar_tabla(connection_values):
    preparar_tablas(connection_values)
    preparar_respuestas(connection_values)
    with connect(**dict(connection_values)) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS cliente")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cliente.registro_whatsapp_entrada (
                    id uuid PRIMARY KEY, whatsapp text NOT NULL, datos jsonb NOT NULL,
                    token_hash text NOT NULL, palabra_hash text NOT NULL,
                    confirmado boolean NOT NULL DEFAULT false, mensaje_id text,
                    creado_en timestamptz NOT NULL DEFAULT now(),
                    vence_en timestamptz NOT NULL DEFAULT (now() + interval '5 minutes')
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS registro_entrada_numero_idx ON cliente.registro_whatsapp_entrada(whatsapp)")


def solicitar_validacion(kwargs, datos, guard, ip):
    secret, _, _, _, numero = configuracion()
    guard.limit(RateLimitItemPerMinute(5, 10), "registro-entrada-ip", ip)
    guard.limit(RateLimitItemPerHour(10), "registro-entrada-numero", datos["whatsapp"])
    guard.limit(RateLimitItemPerHour(60), "registro-entrada-global", "all")
    if len(datos["password"].encode()) > 72:
        raise HTTPException(400, "La contrasena supera el tamano admitido")
    preparar_tabla(tuple(sorted(kwargs.items())))
    id_ = str(uuid4())
    token = secrets.token_urlsafe(32)
    palabra = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
    datos = {**datos, "hash_contrasena": bcrypt.hashpw(datos["password"].encode(), bcrypt.gensalt()).decode()}
    del datos["password"]
    if not datos["correo"]:
        datos["correo"] = f"{id_}@bitstroid.local"
    with connect(**kwargs, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM cliente.registro_whatsapp_entrada WHERE vence_en < now()")
        conn.commit()
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("registro-numero:" + datos["whatsapp"],))
                comprobar_duplicados(cur, datos)
                cur.execute("SELECT 1 FROM cliente.registro_whatsapp_entrada WHERE whatsapp = %s AND creado_en > now() - interval '60 seconds'", (datos["whatsapp"],))
                if cur.fetchone():
                    raise HTTPException(429, "Espera antes de solicitar otra validacion", headers={"Retry-After": "60"})
                cur.execute("DELETE FROM cliente.registro_whatsapp_entrada WHERE whatsapp = %s", (datos["whatsapp"],))
                cur.execute("""INSERT INTO cliente.registro_whatsapp_entrada
                    (id, whatsapp, datos, token_hash, palabra_hash) VALUES (%s, %s, %s::jsonb, %s, %s)""",
                    (id_, datos["whatsapp"], json.dumps(datos), hash_codigo(id_, token, secret), hash_codigo(datos["whatsapp"], palabra, secret)))
    texto = f"Hola Bitstroid, quiero validar mi WhatsApp para crear mi cuenta.\nREGISTRO: {palabra}"
    return {"verificacionId": id_, "verificacionToken": token,
            "whatsappUrl": f"https://wa.me/{numero}?{urlencode({'text': texto})}",
            "venceEn": 300, "reenviarEn": 60}


def mensajes_registro(payload, phone_id):
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        return
    entries = payload.get("entry", [])
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("changes"), list):
            continue
        for change in entry["changes"]:
            if not isinstance(change, dict) or change.get("field") != "messages":
                continue
            value = change.get("value", {})
            if not isinstance(value, dict) or not isinstance(value.get("metadata"), dict):
                continue
            if value["metadata"].get("phone_number_id") != phone_id or value.get("messaging_product") != "whatsapp":
                continue
            messages = value.get("messages", [])
            if not isinstance(messages, list):
                continue
            for message in messages:
                if not isinstance(message, dict) or message.get("type") != "text" or not isinstance(message.get("text"), dict):
                    continue
                text = message["text"].get("body", "")
                sender, message_id = message.get("from", ""), message.get("id", "")
                if not isinstance(text, str) or not isinstance(sender, str) or not isinstance(message_id, str):
                    continue
                match = re.search(r"(?:^|\n)REGISTRO: ([A-HJ-NP-Z2-9]{8})\s*$", text)
                if not match or not re.fullmatch(r"[1-9]\d{7,14}", sender) or not message_id or len(message_id) > 512:
                    continue
                try:
                    timestamp = int(message.get("timestamp", ""))
                    if not 0 < timestamp < 32503680000:
                        continue
                except (ValueError, TypeError, OverflowError):
                    continue
                yield match[1], sender, message_id, timestamp


def procesar_mensajes(kwargs, payload):
    secret, _, _, phone_id, _ = configuracion()
    contactos = list(mensajes_entrantes(payload, phone_id))
    mensajes = list(mensajes_registro(payload, phone_id))
    if not contactos:
        logging.getLogger(__name__).info("Webhook WhatsApp sin mensajes entrantes para el Phone Number ID configurado")
        return
    preparar_tabla(tuple(sorted(kwargs.items())))
    with connect(**kwargs, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            guardar_contactos(cur, contactos, phone_id)
            for palabra, sender, message_id, timestamp in mensajes:
                # Both receipt time and the signed message timestamp must be in the challenge window.
                cur.execute("""UPDATE cliente.registro_whatsapp_entrada SET confirmado = true, mensaje_id = %s
                    WHERE whatsapp = %s AND palabra_hash = %s AND confirmado = false
                    AND vence_en > now() AND to_timestamp(%s) >= date_trunc('second', creado_en)
                    AND to_timestamp(%s) <= now() AND to_timestamp(%s) < vence_en RETURNING id""",
                    (message_id, sender, hash_codigo(sender, palabra, secret), timestamp, timestamp, timestamp))
                valido = bool(cur.fetchone())
                if not valido:
                    cur.execute("""SELECT id FROM cliente.registro_whatsapp_entrada
                        WHERE whatsapp = %s AND palabra_hash = %s AND confirmado = true
                        AND vence_en > now() AND to_timestamp(%s) >= date_trunc('second', creado_en)
                        AND to_timestamp(%s) <= now() AND to_timestamp(%s) < vence_en""",
                        (sender, hash_codigo(sender, palabra, secret), timestamp, timestamp, timestamp))
                    valido = bool(cur.fetchone())
                encolar(cur, message_id, phone_id, sender, "validado" if valido else "invalido", timestamp=timestamp)
                logging.getLogger(__name__).info("Solicitud WhatsApp procesada: %s", "validada" if valido else "invalida o vencida")
            ids_validos = {m[2] for m in mensajes}
            for contacto in contactos:
                mensaje = contacto["mensaje"]
                contenido = mensaje.get("text")
                texto = contenido.get("body", "") if contacto["tipo"] == "text" and isinstance(contenido, dict) else ""
                if isinstance(texto, str) and re.search(r"(?:^|\n)REGISTRO\b", texto) and contacto["mensaje_id"] not in ids_validos:
                    encolar(cur, contacto["mensaje_id"], phone_id, contacto["numero"], "invalido", timestamp=contacto["timestamp"])


def comprobar_solicitud(cur, id_, token, secret, bloquear=False):
    cur.execute("SELECT *, vence_en <= now() AS vencido FROM cliente.registro_whatsapp_entrada WHERE id = %s" + (" FOR UPDATE" if bloquear else ""), (id_,))
    solicitud = cur.fetchone()
    if (not solicitud or solicitud["vencido"] or
            not hmac.compare_digest(solicitud["token_hash"], hash_codigo(id_, token, secret))):
        raise HTTPException(400, "Validacion invalida o vencida. Solicita otra.")
    return solicitud


def estado_validacion(kwargs, id_, token, guard, ip):
    secret = configuracion()[0]
    guard.limit(RateLimitItemPerMinute(60), "registro-estado-ip", ip)
    preparar_tabla(tuple(sorted(kwargs.items())))
    with connect(**kwargs, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            solicitud = comprobar_solicitud(cur, id_, token, secret)
    return {"confirmado": solicitud["confirmado"]}


def crear_registro(kwargs, id_, token, guard, ip):
    secret = configuracion()[0]
    guard.limit(RateLimitItemPerMinute(15), "registro-final-ip", ip)
    preparar_tabla(tuple(sorted(kwargs.items())))
    with connect(**kwargs, row_factory=dict_row) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                solicitud = comprobar_solicitud(cur, id_, token, secret)
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("registro-numero:" + solicitud["whatsapp"],))
                solicitud = comprobar_solicitud(cur, id_, token, secret, bloquear=True)
                if not solicitud["confirmado"]:
                    raise HTTPException(409, "Tu WhatsApp aun no esta validado")
                datos = solicitud["datos"]
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("registro-usuario:" + datos["apodo"].lower(),))
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("registro-correo:" + datos["correo"].lower(),))
                comprobar_duplicados(cur, datos)
                usuario_id = str(uuid4())
                cur.execute("""INSERT INTO seguridad.usuario (id, apodo, nombres, correo, whatsapp, hash_contrasena,
                    forzar_cambio_contrasena, correo_verificado, whatsapp_verificado, estado)
                    VALUES (%s, %s, NULLIF(%s, ''), %s, %s, %s, false, false, true, 'activo')
                    RETURNING id, apodo, nombres, correo, whatsapp""",
                    (usuario_id, datos["apodo"], datos["nombres"], datos["correo"], datos["whatsapp"], datos["hash_contrasena"]))
                row = cur.fetchone()
                cur.execute("INSERT INTO seguridad.categoria_rol (usuario_id, rol, activo) VALUES (%s, 'cliente', true) ON CONFLICT DO NOTHING", (usuario_id,))
                cur.execute("INSERT INTO cliente.cliente (usuario_id, puntos, activo, creado_por_usuario_id) VALUES (%s, 0, true, %s) ON CONFLICT (usuario_id) DO NOTHING", (usuario_id, usuario_id))
                vincular_usuario(cur, datos["whatsapp"], usuario_id)
                encolar(cur, "cuenta:" + usuario_id, configuracion()[3], datos["whatsapp"], "creado")
                cur.execute("DELETE FROM cliente.registro_whatsapp_entrada WHERE id = %s", (id_,))
    return row
