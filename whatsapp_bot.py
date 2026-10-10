"""Interactive service menu, durable replies and per-contact human handoff."""
from datetime import datetime, timezone
from functools import cache
import json
import logging
from os import getenv
import re
import secrets
import time
from uuid import uuid4

from db_compat import connect, dict_row
from whatsapp_entregas import preparar_tablas as preparar_entregas, datos_envio, crear_solicitud, fecha_contraentrega
from whatsapp_respuestas import enviar_payload
from whatsapp_entregas import LIMA

logger = logging.getLogger("uvicorn.error")
_ultima_limpieza = 0.0
FIELDS = [("nombre_completo", "Nombre completo del destinatario"), ("dni", "DNI o documento del destinatario"),
          ("departamento", "Departamento"), ("provincia", "Provincia"), ("distrito", "Distrito"),
          ("direccion", "Direccion completa"), ("referencia", "Referencia (escribe - si no hay)"),
          ("contacto", "Telefono del destinatario, con codigo de pais"), ("agencia_shalom", "Direccion de la agencia Shalom")]


def habilitado():
    return getenv("WHATSAPP_BOT_ENABLED", "false").lower() == "true"


@cache
def preparar_tablas(connection_values):
    preparar_entregas(connection_values)
    with connect(**dict(connection_values)) as conn:
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS whatsapp.bot_sesion (
                phone_number_id text NOT NULL, numero text NOT NULL,
                contacto_id uuid NOT NULL REFERENCES whatsapp.contacto(id) ON DELETE CASCADE,
                ultima_actividad timestamptz NOT NULL, ultimo_cliente timestamptz NOT NULL,
                pausado boolean NOT NULL DEFAULT false, token text NOT NULL,
                paso text NOT NULL DEFAULT 'menu', datos jsonb NOT NULL DEFAULT '{}',
                PRIMARY KEY (phone_number_id, numero)
            )""")
            cur.execute("""CREATE TABLE IF NOT EXISTS whatsapp.bot_evento (
                phone_number_id text NOT NULL, mensaje_id text NOT NULL,
                creado_en timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (phone_number_id, mensaje_id)
            )""")
            cur.execute("""CREATE TABLE IF NOT EXISTS whatsapp.bot_respuesta (
                id bigserial PRIMARY KEY, phone_number_id text NOT NULL, numero text NOT NULL,
                token text NOT NULL, payload jsonb NOT NULL,
                disponible_en timestamptz NOT NULL DEFAULT now(),
                intentos integer NOT NULL DEFAULT 0, enviado_en timestamptz,
                cancelado boolean NOT NULL DEFAULT false, creado_en timestamptz NOT NULL DEFAULT now()
            )""")
            cur.execute("CREATE INDEX IF NOT EXISTS bot_respuesta_pendiente_idx ON whatsapp.bot_respuesta(disponible_en) WHERE enviado_en IS NULL AND cancelado = false")


def botones(texto, opciones, token):
    return {"type": "interactive", "interactive": {"type": "button", "body": {"text": texto[:1024]},
        "action": {"buttons": [{"type": "reply", "reply": {"id": f"{token}:{key}", "title": title}}
                               for key, title in opciones]}}}


def lista(texto, opciones, token):
    return {"type": "interactive", "interactive": {"type": "list", "body": {"text": texto[:1024]},
        "action": {"button": "Ver opciones", "sections": [{"title": "Bitstroid", "rows": [
            {"id": f"{token}:{key}", "title": title} for key, title in opciones]}]}}}


def menu(token, saludo=False):
    texto = ("Hola, bienvenido a Bitstroid. En breve te atenderemos. Mientras tanto, te dejamos algunas opciones. "
             "Si no encuentras lo que necesitas, puedes escribir tu consulta." if saludo else "Puedes elegir otra opcion o escribir tu consulta.")
    return lista(texto, [("compras", "Mis compras"), ("entrega", "Solicitar entrega")], token)


def texto(body):
    return {"type": "text", "text": {"body": body[:4096]}}


def es_registro(message):
    body = message.get("text", {}).get("body", "") if isinstance(message.get("text"), dict) else ""
    return isinstance(body, str) and bool(re.search(r"(?:^|\n)\s*REGISTRO\b", body, re.I))


def ecos_humanos(payload, phone_id):
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        return
    for entry in payload.get("entry", []) if isinstance(payload.get("entry"), list) else []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes", []) if isinstance(entry.get("changes"), list) else []:
            if not isinstance(change, dict) or change.get("field") != "smb_message_echoes":
                continue
            value = change.get("value", {})
            if (not isinstance(value, dict) or not isinstance(value.get("metadata"), dict)
                    or value["metadata"].get("phone_number_id") != phone_id):
                continue
            for m in value.get("message_echoes", []) if isinstance(value.get("message_echoes"), list) else []:
                if not isinstance(m, dict):
                    continue
                try:
                    stamp = int(m.get("timestamp", ""))
                except (ValueError, TypeError):
                    continue
                if (isinstance(m.get("to"), str) and re.fullmatch(r"[1-9][0-9]{7,14}", m["to"])
                        and isinstance(m.get("id"), str) and 0 < len(m["id"]) <= 512
                        and 0 < stamp <= time.time() + 60):
                    yield {"numero": m["to"], "mensaje_id": m["id"], "timestamp": stamp, "humano": True}


def seleccion(message, token):
    interactive = message.get("interactive")
    if not isinstance(interactive, dict):
        return None
    reply = interactive.get(interactive.get("type"))
    id_ = reply.get("id", "") if isinstance(reply, dict) else ""
    return id_[len(token) + 1:] if isinstance(id_, str) and id_.startswith(token + ":") else None


def encolar(cur, session, payload, demora=0):
    cur.execute("""INSERT INTO whatsapp.bot_respuesta (phone_number_id, numero, token, payload, disponible_en)
        VALUES (%s, %s, %s, %s::jsonb, now() + %s * interval '1 second')""",
        (session["phone_number_id"], session["numero"], session["token"], json.dumps(payload), demora))


def compras(cur, contacto_id):
    # Never match by display name: only an active account with the verified sender number.
    cur.execute("""SELECT v.id, v.fecha_venta, v.total FROM venta.venta v
        JOIN whatsapp.contacto c ON c.id = %s
        JOIN seguridad.usuario u ON u.id = c.usuario_id AND u.id = v.cliente_usuario_id
        JOIN cliente.cliente cl ON cl.usuario_id = u.id
        WHERE u.whatsapp = c.numero AND u.whatsapp_verificado = true
        AND u.estado = 'activo' AND cl.activo = true
        ORDER BY v.fecha_venta DESC LIMIT 10""", (contacto_id,))
    rows = cur.fetchall()
    if not rows:
        return "No encontramos compras vinculadas a una cuenta verificada con este numero. Podemos revisarlo cuando te atendamos."
    lines = ["*Tus ultimas compras*"]
    for row in rows:
        cur.execute("SELECT item_nombre, cantidad FROM venta.venta_item WHERE venta_id = %s ORDER BY id LIMIT 12", (row["id"],))
        names = ", ".join(f"{r['item_nombre']} x{r['cantidad']}" for r in cur.fetchall())
        lines.append(f"{row['fecha_venta'].astimezone(LIMA).date():%d/%m/%Y}: {names}\nTotal: S/ {row['total']:.2f}")
    return "\n\n".join(lines)[:4000]


def campos_envio(modalidad):
    excluir = {"direccion", "referencia"} if modalidad == "shalom" else {"agencia_shalom"}
    return [(key, label) for key, label in FIELDS if key not in excluir]


def datos_completos(d, modalidad):
    return all(str(d.get(key) or "").strip() for key, _ in campos_envio(modalidad) if key != "referencia")


def resumen(d, modalidad):
    return "\n".join(f"{label}: {d.get(key) or '-'}" for key, label in campos_envio(modalidad))


def preguntar(cur, s):
    fields = campos_envio(s["datos"]["modalidad"])
    index = s["datos"]["campo"]
    if index < len(fields):
        encolar(cur, s, botones(fields[index][1] + ":", [("menu", "Cancelar")], s["token"]))
    else:
        s["paso"] = "confirmar_datos"
        encolar(cur, s, botones("Confirma los datos de envio:\n" + resumen(s["datos"]["direccion"], s["datos"]["modalidad"]),
                               [("guardar", "Confirmar datos"), ("nuevos", "Cambiar datos"), ("menu", "Volver")], s["token"]))


def atender(cur, s, opcion, message, recibido):
    token = s["token"]
    if opcion == "menu":
        s["paso"], s["datos"] = "menu", {}
        encolar(cur, s, menu(token))
    elif s["paso"] == "menu" and opcion == "compras":
        encolar(cur, s, texto(compras(cur, s["contacto_id"])))
        encolar(cur, s, menu(token), 5)
    elif s["paso"] == "menu" and opcion == "entrega":
        s["paso"] = "entrega"
        encolar(cur, s, lista("Elige la modalidad de entrega.", [("contraentrega", "Contraentrega"), ("olva", "Envio por Olva"), ("shalom", "Envio por Shalom")], token))
    elif s["paso"] == "entrega" and opcion in ("contraentrega", "olva", "shalom"):
        s["paso"], s["datos"] = "confirmar_entrega", {"modalidad": opcion}
        if opcion == "contraentrega":
            body = ("Entrega en Feria Grau, sabados de 1 pm a 6 pm. Solicitudes hasta el viernes a las 5 pm; "
                    "despues se entregan la siguiente semana. Fecha prevista: " + fecha_contraentrega().strftime("%d/%m/%Y") + ".")
        else:
            body = ("El tarifario varia entre distritos de Lima y provincias. Confirmaremos el costo antes del despacho. " +
                    ("Olva: el envio se paga antes del despacho." if opcion == "olva" else "Shalom: Pago destino, pagas el envio al recoger en tu agencia."))
        encolar(cur, s, botones(body, [("solicitar", "Solicitar entrega"), ("menu", "Volver")], token))
    elif s["paso"] == "confirmar_entrega" and opcion == "solicitar":
        solicitud = crear_solicitud(cur, s["contacto_id"], s["datos"]["modalidad"], s["phone_number_id"], message["id"])
        s["datos"]["solicitud_id"] = solicitud["solicitudId"]
        if s["datos"]["modalidad"] == "contraentrega":
            s["paso"], s["datos"] = "menu", {}
            encolar(cur, s, texto("Solicitud recibida para el " + solicitud["fechaEntrega"] + ". Te confirmaremos la entrega; aun no esta confirmada."))
            encolar(cur, s, menu(token), 5)
        else:
            destinos = [d for d in solicitud["destinatarios"] if datos_completos(d, s["datos"]["modalidad"])]
            if destinos:
                s["paso"] = "datos_previos"
                s["datos"]["destinatario_id"] = str(destinos[0]["id"])
                encolar(cur, s, botones("Datos guardados para " + s["datos"]["modalidad"].capitalize() + ":\n" + resumen(destinos[0], s["datos"]["modalidad"]),
                    [("usar", "Usar estos datos"), ("nuevos", "Cambiar datos")], token))
            else:
                iniciar_formulario(cur, s)
    elif s["paso"] in ("datos_previos", "confirmar_datos") and opcion == "nuevos":
        iniciar_formulario(cur, s)
    elif s["paso"] == "datos_previos" and opcion == "usar":
        destinos = datos_envio(cur, s["contacto_id"])
        destino = next((d for d in destinos if str(d["id"]) == s["datos"]["destinatario_id"]), None)
        if not destino or not datos_completos(destino, s["datos"]["modalidad"]):
            iniciar_formulario(cur, s)
        else:
            confirmar_destino(cur, s, str(destino["id"]))
    elif s["paso"] == "formulario" and message.get("type") == "text":
        value = message.get("text", {}).get("body", "").strip()
        fields = campos_envio(s["datos"]["modalidad"])
        key = fields[s["datos"]["campo"]][0]
        limite = {"dni": 20, "contacto": 30, "nombre_completo": 180, "agencia_shalom": 180}.get(key, 120 if key in ("departamento", "provincia", "distrito") else 400)
        if not value or len(value) > limite or (key == "contacto" and not re.fullmatch(r"\+?[1-9][0-9]{7,14}", value)):
            encolar(cur, s, texto("Dato invalido. Escribe nuevamente (telefono con codigo de pais, sin espacios)."))
        else:
            s["datos"]["direccion"][key] = "" if key == "referencia" and value == "-" else value
            s["datos"]["campo"] += 1
            preguntar(cur, s)
    elif s["paso"] == "confirmar_datos" and opcion == "guardar":
        d = s["datos"]["direccion"]
        id_ = str(uuid4())
        cur.execute("""INSERT INTO cliente.destinatario_envio
            (id, contacto_whatsapp_id, nombre_completo, dni, departamento, provincia, distrito,
             direccion, referencia, contacto, agencia_shalom)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (id_, s["contacto_id"], *(d.get(k, "") for k in ("nombre_completo", "dni", "departamento", "provincia", "distrito", "direccion", "referencia", "contacto", "agencia_shalom"))))
        confirmar_destino(cur, s, id_)


def iniciar_formulario(cur, s):
    s["paso"] = "formulario"
    s["datos"].update(campo=0, direccion={})
    encolar(cur, s, botones("Registraremos los datos del destinatario por mensajes. Puedes cancelar para dejar tu consulta.", [("menu", "Cancelar")], s["token"]))
    preguntar(cur, s)


def confirmar_destino(cur, s, id_):
    cur.execute("""UPDATE whatsapp.solicitud_entrega SET destinatario_id = %s,
        estado = 'pendiente', actualizado_en = now() WHERE id = %s AND contacto_id = %s
        AND estado = 'pendiente_datos'""", (id_, s["datos"]["solicitud_id"], s["contacto_id"]))
    s["paso"], s["datos"] = "menu", {}
    encolar(cur, s, texto("Datos guardados y solicitud recibida. Te confirmaremos el despacho; aun no esta confirmado."))
    encolar(cur, s, menu(s["token"]), 5)


def procesar(kwargs, payload, contactos, phone_id):
    if not habilitado():
        return
    preparar_tablas(tuple(sorted(kwargs.items())))
    eventos = contactos + list(ecos_humanos(payload, phone_id))
    with connect(**kwargs, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            for e in sorted(eventos, key=lambda e: (e["numero"], e["timestamp"], not e.get("humano", False))):
                if not e.get("humano") and es_registro(e["mensaje"]):
                    continue
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("wa-bot:" + phone_id + ":" + e["numero"],))
                cur.execute("""INSERT INTO whatsapp.bot_evento (phone_number_id, mensaje_id)
                    VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING mensaje_id""", (phone_id, e["mensaje_id"]))
                if not cur.fetchone():
                    continue
                stamp = datetime.fromtimestamp(e["timestamp"], timezone.utc)
                cur.execute("SELECT id FROM whatsapp.contacto WHERE numero = %s", (e["numero"],))
                contacto = cur.fetchone()
                if not contacto:
                    # Human reply can precede the first delivered customer webhook.
                    cur.execute("""INSERT INTO whatsapp.contacto (id, numero, primer_mensaje_en, ultimo_mensaje_en)
                        VALUES (%s, %s, %s, %s) RETURNING id""", (str(uuid4()), e["numero"], stamp, stamp))
                    contacto = cur.fetchone()
                cur.execute("SELECT * FROM whatsapp.bot_sesion WHERE phone_number_id = %s AND numero = %s FOR UPDATE", (phone_id, e["numero"]))
                s = cur.fetchone()
                nuevo = not s or (stamp - s["ultima_actividad"]).total_seconds() >= 3600
                if s and stamp < s["ultima_actividad"] and (not e.get("humano") or (s["ultima_actividad"] - stamp).total_seconds() >= 3600):
                    continue
                if not s:
                    s = {"phone_number_id": phone_id, "numero": e["numero"], "contacto_id": contacto["id"],
                         "ultima_actividad": stamp, "ultimo_cliente": datetime(1970, 1, 1, tzinfo=timezone.utc),
                         "token": secrets.token_hex(8), "paso": "menu", "datos": {}, "pausado": False}
                if isinstance(s["datos"], str):
                    s["datos"] = json.loads(s["datos"])
                if e.get("humano"):
                    s.update(pausado=True, paso="menu", datos={}, token=secrets.token_hex(8))
                    logger.info("Bot WhatsApp pausado por respuesta humana")
                elif time.time() - e["timestamp"] > 300:
                    continue
                else:
                    s["ultimo_cliente"] = stamp
                    if nuevo:
                        s.update(pausado=False, paso="menu", datos={}, token=secrets.token_hex(8))
                        encolar(cur, s, {"status": "read", "message_id": e["mensaje_id"], "typing_indicator": {"type": "text"}})
                        encolar(cur, s, menu(s["token"], True), 3)
                    elif not s["pausado"]:
                        option = seleccion(e["mensaje"], s["token"])
                        if option or (s["paso"] == "formulario" and e["tipo"] == "text"):
                            cur.execute("""SELECT count(*) AS cantidad FROM whatsapp.bot_respuesta
                                WHERE phone_number_id = %s AND numero = %s AND creado_en > now() - interval '1 minute'""", (phone_id, s["numero"]))
                            if cur.fetchone()["cantidad"] < 20:
                                s["token"] = secrets.token_hex(8)
                                atender(cur, s, option, e["mensaje"], stamp)
                s["ultima_actividad"] = max(stamp, s["ultima_actividad"])
                cur.execute("""INSERT INTO whatsapp.bot_sesion
                    (phone_number_id, numero, contacto_id, ultima_actividad, ultimo_cliente, pausado, token, paso, datos)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (phone_number_id, numero) DO UPDATE SET ultima_actividad = EXCLUDED.ultima_actividad,
                    ultimo_cliente = EXCLUDED.ultimo_cliente, pausado = EXCLUDED.pausado, token = EXCLUDED.token,
                    paso = EXCLUDED.paso, datos = EXCLUDED.datos""",
                    (phone_id, s["numero"], s["contacto_id"], stamp, s["ultimo_cliente"], s["pausado"], s["token"], s["paso"], json.dumps(s["datos"])))
                cur.execute("""UPDATE whatsapp.bot_respuesta SET cancelado = true, payload = '{}'::jsonb WHERE phone_number_id = %s
                    AND numero = %s AND token <> %s AND enviado_en IS NULL""", (phone_id, s["numero"], s["token"]))


def limpiar(kwargs):
    global _ultima_limpieza
    if time.monotonic() - _ultima_limpieza < 600:
        return
    with connect(**kwargs) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM whatsapp.bot_respuesta WHERE creado_en < now() - interval '7 days'")
            cur.execute("DELETE FROM whatsapp.bot_evento WHERE creado_en < now() - interval '7 days'")
            cur.execute("""UPDATE whatsapp.bot_sesion SET datos = '{}'::jsonb
                WHERE ultima_actividad < now() - interval '1 hour' AND datos <> '{}'::jsonb""")
    _ultima_limpieza = time.monotonic()


def despachar(kwargs):
    if not habilitado():
        return
    preparar_tablas(tuple(sorted(kwargs.items())))
    limpiar(kwargs)
    with connect(**kwargs, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT r.* FROM whatsapp.bot_respuesta r JOIN whatsapp.bot_sesion s
                ON s.phone_number_id = r.phone_number_id AND s.numero = r.numero AND s.token = r.token
                WHERE r.enviado_en IS NULL AND r.cancelado = false AND r.intentos < 3
                AND r.disponible_en <= now() AND s.pausado = false
                AND s.ultimo_cliente > now() - interval '23 hours'
                AND NOT EXISTS (SELECT 1 FROM whatsapp.bot_respuesta anterior WHERE anterior.phone_number_id = r.phone_number_id
                    AND anterior.numero = r.numero AND anterior.id < r.id AND anterior.token = r.token
                    AND anterior.enviado_en IS NULL AND anterior.cancelado = false AND anterior.intentos < 3)
                ORDER BY r.id LIMIT 1 FOR UPDATE OF r SKIP LOCKED""")
            r = cur.fetchone()
            if not r:
                return
            cur.execute("SELECT pg_try_advisory_xact_lock(hashtext(%s)) AS adquirido", ("wa-bot:" + r["phone_number_id"] + ":" + r["numero"],))
            if not cur.fetchone()["adquirido"]:
                return
            cur.execute("SELECT token, pausado FROM whatsapp.bot_sesion WHERE phone_number_id = %s AND numero = %s", (r["phone_number_id"], r["numero"]))
            s = cur.fetchone()
            if s["pausado"] or s["token"] != r["token"]:
                return
            payload = json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"]
            try:
                enviar_payload(r["numero"], payload, r["phone_number_id"])
            except Exception as error:
                logger.warning("Bot WhatsApp no enviado: id=%s error=%s", r["id"], type(error).__name__)
                # Typing is optional; failure must not hold up the greeting.
                cur.execute("""UPDATE whatsapp.bot_respuesta SET intentos = intentos + 1,
                    cancelado = %s, disponible_en = now() + interval '30 seconds' WHERE id = %s""", ("status" in payload, r["id"]))
            else:
                cur.execute("UPDATE whatsapp.bot_respuesta SET enviado_en = now(), payload = '{}'::jsonb WHERE id = %s", (r["id"],))
                logger.info("Bot WhatsApp aceptado por Meta: id=%s", r["id"])
