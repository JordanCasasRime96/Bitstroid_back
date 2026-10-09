"""Contact metadata only: never persist message bodies or registration secrets."""
from functools import cache
import re
from time import time
from uuid import uuid4

from db_compat import connect


@cache
def preparar_tablas(connection_values):
    with connect(**dict(connection_values)) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS whatsapp")
            cur.execute("""CREATE TABLE IF NOT EXISTS whatsapp.contacto (
                id uuid PRIMARY KEY,
                numero text NOT NULL UNIQUE CHECK (numero ~ '^[1-9][0-9]{7,14}$'),
                nombre_perfil varchar(160) NOT NULL DEFAULT '',
                usuario_id uuid REFERENCES seguridad.usuario(id) ON DELETE SET NULL,
                primer_mensaje_en timestamptz NOT NULL,
                ultimo_mensaje_en timestamptz NOT NULL,
                total_mensajes bigint NOT NULL DEFAULT 0 CHECK (total_mensajes >= 0),
                creado_en timestamptz NOT NULL DEFAULT now(),
                actualizado_en timestamptz NOT NULL DEFAULT now()
            )""")
            cur.execute("""CREATE TABLE IF NOT EXISTS whatsapp.mensaje_entrante (
                phone_number_id text NOT NULL,
                mensaje_id varchar(512) NOT NULL,
                contacto_id uuid NOT NULL REFERENCES whatsapp.contacto(id) ON DELETE CASCADE,
                tipo varchar(32) NOT NULL,
                enviado_en timestamptz NOT NULL,
                recibido_en timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (phone_number_id, mensaje_id)
            )""")
            cur.execute("CREATE INDEX IF NOT EXISTS contacto_ultimo_mensaje_idx ON whatsapp.contacto(ultimo_mensaje_en DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS contacto_usuario_idx ON whatsapp.contacto(usuario_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS mensaje_contacto_idx ON whatsapp.mensaje_entrante(contacto_id, enviado_en DESC)")


def mensajes_entrantes(payload, phone_id):
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
            profiles = {}
            contacts = value.get("contacts", [])
            if isinstance(contacts, list):
                for contact in contacts:
                    if not isinstance(contact, dict) or not isinstance(contact.get("wa_id"), str):
                        continue
                    profile = contact.get("profile", {})
                    name = profile.get("name") if isinstance(profile, dict) else None
                    if isinstance(name, str):
                        profiles[contact["wa_id"]] = name.strip()[:160]
            messages = value.get("messages", [])
            if not isinstance(messages, list):
                continue
            for message in messages:
                if not isinstance(message, dict):
                    continue
                sender, message_id, kind = message.get("from"), message.get("id"), message.get("type")
                if (not isinstance(sender, str) or not re.fullmatch(r"[1-9]\d{7,14}", sender)
                        or not isinstance(message_id, str) or not 1 <= len(message_id) <= 512
                        or not isinstance(kind, str) or not re.fullmatch(r"[a-z_]{1,32}", kind)):
                    continue
                try:
                    timestamp = int(message.get("timestamp", ""))
                    if not 0 < timestamp <= time() + 60:
                        continue
                except (ValueError, TypeError, OverflowError):
                    continue
                yield {"numero": sender, "mensaje_id": message_id, "tipo": kind,
                    "timestamp": timestamp, "nombre_perfil": profiles.get(sender, ""), "mensaje": message}


def guardar_contactos(cur, mensajes, phone_id):
    # Stable lock ordering avoids deadlocks between batched webhook retries.
    for message in sorted(mensajes, key=lambda item: (item["numero"], item["mensaje_id"])):
        numero, timestamp = message["numero"], message["timestamp"]
        cur.execute("""INSERT INTO whatsapp.contacto
            (id, numero, primer_mensaje_en, ultimo_mensaje_en)
            VALUES (%s, %s, to_timestamp(%s), to_timestamp(%s))
            ON CONFLICT (numero) DO UPDATE SET numero = EXCLUDED.numero RETURNING id""",
            (str(uuid4()), numero, timestamp, timestamp))
        contacto_id = cur.fetchone()["id"]
        cur.execute("""INSERT INTO whatsapp.mensaje_entrante
            (phone_number_id, mensaje_id, contacto_id, tipo, enviado_en)
            VALUES (%s, %s, %s, %s, to_timestamp(%s))
            ON CONFLICT (phone_number_id, mensaje_id) DO NOTHING RETURNING contacto_id""",
            (phone_id, message["mensaje_id"], contacto_id, message["tipo"], timestamp))
        if not cur.fetchone():
            continue
        cur.execute("""UPDATE whatsapp.contacto SET
            nombre_perfil = CASE WHEN %s <> '' AND to_timestamp(%s) >= ultimo_mensaje_en
                THEN %s ELSE nombre_perfil END,
            primer_mensaje_en = LEAST(primer_mensaje_en, to_timestamp(%s)),
            ultimo_mensaje_en = GREATEST(ultimo_mensaje_en, to_timestamp(%s)),
            total_mensajes = total_mensajes + 1, actualizado_en = now(),
            usuario_id = COALESCE(usuario_id, (
                SELECT CASE WHEN count(*) = 1 THEN (array_agg(u.id))[1] ELSE NULL END
                FROM seguridad.usuario u JOIN cliente.cliente c ON c.usuario_id = u.id
                WHERE u.whatsapp = %s AND u.whatsapp_verificado = true
                  AND u.estado = 'activo' AND c.activo = true
            )) WHERE id = %s""", (message["nombre_perfil"], timestamp, message["nombre_perfil"],
                timestamp, timestamp, numero, contacto_id))


def vincular_usuario(cur, numero, usuario_id):
    cur.execute("""UPDATE whatsapp.contacto SET usuario_id = %s, actualizado_en = now()
        WHERE numero = %s AND (usuario_id IS NULL OR usuario_id = %s)""",
        (usuario_id, numero, usuario_id))
