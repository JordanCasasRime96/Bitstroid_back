"""Durable, deduplicated service replies; never store codes or access tokens."""
from functools import cache
import json
import logging
from os import getenv
import re
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from db_compat import connect, dict_row

logger = logging.getLogger("uvicorn.error")
TEXTOS = {
    "validado": "Hola, tu WhatsApp ha sido validado. Vuelve a Bitstroid para terminar de crear tu cuenta.",
    "invalido": "No pudimos validar tu solicitud. Comprueba que escribes desde el mismo numero indicado en Bitstroid y solicita una nueva validacion; dura 5 minutos.",
    "creado": "Hola, tu cuenta de Bitstroid se creo correctamente.",
}


def diagnostico_db(error):
    datos = next((arg for arg in error.args if isinstance(arg, dict)), {})
    codigo = datos.get("C", "")
    if not isinstance(codigo, str) or not re.fullmatch(r"[0-9A-Z]{5}", codigo):
        return type(error).__name__
    causas = {
        "42P01": "Falta una tabla; aplicar las migraciones SQL en la BD del backend",
        "42703": "Falta una columna; comprobar la estructura de whatsapp.respuesta_registro",
        "42702": "Una columna de la consulta es ambigua",
        "42501": "El usuario de la BD no tiene los permisos necesarios",
        "28P01": "Credenciales de la BD incorrectas",
        "3D000": "La base de datos configurada no existe",
        "53300": "Se alcanzo el limite de conexiones de PostgreSQL",
        "57014": "PostgreSQL cancelo la consulta por tiempo o interrupcion",
    }
    return f"{type(error).__name__} SQLSTATE={codigo}: {causas.get(codigo, 'Error de PostgreSQL')}"


@cache
def preparar_tabla(connection_values):
    with connect(**dict(connection_values)) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS whatsapp")
            cur.execute("""CREATE TABLE IF NOT EXISTS whatsapp.respuesta_registro (
                id bigserial PRIMARY KEY, evento text NOT NULL, phone_number_id text NOT NULL,
                numero text NOT NULL, tipo text NOT NULL CHECK (tipo IN ('validado', 'invalido', 'creado')),
                creado_en timestamptz NOT NULL DEFAULT now(),
                recibido_en timestamptz NOT NULL DEFAULT now(),
                proximo_intento timestamptz NOT NULL DEFAULT now(), intentos integer NOT NULL DEFAULT 0,
                enviado_en timestamptz, UNIQUE (phone_number_id, evento)
            )""")
            # CREATE IF NOT EXISTS does not upgrade previously deployed tables.
            # Unknown timestamps from the old queue must not open a new reply window.
            cur.execute("ALTER TABLE whatsapp.respuesta_registro ADD COLUMN IF NOT EXISTS recibido_en timestamptz NOT NULL DEFAULT '-infinity'::timestamptz")
            cur.execute("ALTER TABLE whatsapp.respuesta_registro ALTER COLUMN recibido_en SET DEFAULT now()")


def encolar(cur, evento, phone_id, numero, tipo, timestamp=None):
    # Limit automated feedback per sender, including requests with bad codes.
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("wa-respuesta:" + numero,))
    cur.execute("""INSERT INTO whatsapp.respuesta_registro (evento, phone_number_id, numero, tipo, recibido_en)
        SELECT %s, %s, %s, %s, to_timestamp(%s) WHERE %s = 'creado' OR
        (SELECT count(*) FROM whatsapp.respuesta_registro
         WHERE numero = %s AND creado_en > now() - interval '1 minute') < 5
        ON CONFLICT (phone_number_id, evento) DO NOTHING""",
        (evento, phone_id, numero, tipo, timestamp if timestamp is not None else time.time(), tipo, numero))


def enviar(numero, tipo, phone_id):
    return enviar_payload(numero, {"type": "text", "text": {"body": TEXTOS[tipo]}}, phone_id)


def enviar_payload(numero, payload, phone_id):
    token = getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
    version = getenv("WHATSAPP_API_VERSION", "v21.0").strip()
    if not token or not re.fullmatch(r"v\d+\.\d+", version) or not phone_id.isdigit():
        raise ValueError("Configuracion de envio de WhatsApp incompleta")
    body = json.dumps({"messaging_product": "whatsapp", **({"to": numero} if "status" not in payload else {}), **payload}).encode()
    request = Request(f"https://graph.facebook.com/{version}/{phone_id}/messages", data=body,
                      headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urlopen(request, timeout=8) as response:
        result = json.load(response)
    if payload.get("status") == "read" and isinstance(result, dict) and result.get("success"):
        return result
    if not isinstance(result, dict) or not result.get("messages", [{}])[0].get("id"):
        raise ValueError("WhatsApp no confirmo la aceptacion del mensaje")
    return result


def despachar(kwargs):
    preparar_tabla(tuple(sorted(kwargs.items())))
    # Claim and commit before calling Meta so multiple workers cannot send concurrently.
    with connect(**kwargs, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("""WITH pendiente AS (
                SELECT id FROM whatsapp.respuesta_registro WHERE enviado_en IS NULL
                AND intentos < 5 AND proximo_intento <= now()
                AND recibido_en > now() - interval '23 hours'
                ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED
            ) UPDATE whatsapp.respuesta_registro r SET intentos = intentos + 1,
                proximo_intento = now() + interval '60 seconds'
                FROM pendiente p WHERE r.id = p.id RETURNING r.*""")
            row = cur.fetchone()
    if not row:
        return
    try:
        enviar(row["numero"], row["tipo"], row["phone_number_id"])
    except Exception as error:
        # Provider bodies can contain personal information; log only safe diagnostics.
        codigo = error.code if isinstance(error, HTTPError) else type(error).__name__
        if isinstance(error, HTTPError):
            try:
                proveedor = json.loads(error.read(16384)).get("error", {}).get("code")
                if isinstance(proveedor, int):
                    codigo = f"HTTP {error.code}, Meta {proveedor}"
            except (ValueError, AttributeError, OSError):
                pass
        logger.warning("Respuesta de registro WhatsApp no enviada: id=%s error=%s", row["id"], codigo)
        return
    with connect(**kwargs) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE whatsapp.respuesta_registro SET enviado_en = now() WHERE id = %s", (row["id"],))
    logger.info("Respuesta de registro WhatsApp aceptada por Meta: id=%s tipo=%s", row["id"], row["tipo"])
