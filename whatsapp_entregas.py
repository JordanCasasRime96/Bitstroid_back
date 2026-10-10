"""Delivery foundation; Flow processing and the address form are separate stages."""
from datetime import datetime, time, timedelta
from functools import cache
from uuid import uuid4
from zoneinfo import ZoneInfo

from db_compat import connect

LIMA = ZoneInfo("America/Lima")
PAGO_ENVIO = {"contraentrega": "no_aplica", "olva": "anticipado", "shalom": "destino"}


@cache
def preparar_tablas(connection_values):
    with connect(**dict(connection_values)) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS cliente")
            cur.execute("""CREATE TABLE IF NOT EXISTS cliente.destinatario_envio (
                id uuid PRIMARY KEY,
                usuario_id uuid NOT NULL REFERENCES seguridad.usuario(id) ON DELETE CASCADE,
                nombre_completo varchar(180) NOT NULL, dni varchar(20) NOT NULL,
                direccion text NOT NULL, referencia text NOT NULL DEFAULT '',
                provincia varchar(120) NOT NULL, distrito varchar(120) NOT NULL,
                contacto varchar(30) NOT NULL, predeterminado boolean NOT NULL DEFAULT false,
                activo boolean NOT NULL DEFAULT true, creado_en timestamptz NOT NULL DEFAULT now()
            )""")
            cur.execute("ALTER TABLE cliente.destinatario_envio ADD COLUMN IF NOT EXISTS contacto_whatsapp_id uuid REFERENCES whatsapp.contacto(id) ON DELETE CASCADE")
            cur.execute("ALTER TABLE cliente.destinatario_envio ALTER COLUMN usuario_id DROP NOT NULL")
            cur.execute("ALTER TABLE cliente.destinatario_envio ADD COLUMN IF NOT EXISTS departamento varchar(120) NOT NULL DEFAULT ''")
            cur.execute("ALTER TABLE cliente.destinatario_envio ADD COLUMN IF NOT EXISTS agencia_shalom varchar(180) NOT NULL DEFAULT ''")
            cur.execute("CREATE INDEX IF NOT EXISTS destinatario_contacto_whatsapp_idx ON cliente.destinatario_envio(contacto_whatsapp_id)")
            cur.execute("""CREATE TABLE IF NOT EXISTS whatsapp.solicitud_entrega (
                id uuid PRIMARY KEY, contacto_id uuid NOT NULL REFERENCES whatsapp.contacto(id) ON DELETE CASCADE,
                destinatario_id uuid REFERENCES cliente.destinatario_envio(id) ON DELETE SET NULL,
                modalidad varchar(20) NOT NULL CHECK (modalidad IN ('contraentrega', 'olva', 'shalom')),
                pago_envio varchar(20) NOT NULL CHECK (pago_envio IN ('no_aplica', 'anticipado', 'destino')),
                estado varchar(20) NOT NULL DEFAULT 'pendiente' CHECK (estado IN ('pendiente_datos', 'pendiente', 'confirmado', 'despachado', 'entregado', 'cancelado')),
                fecha_entrega date, phone_number_id text NOT NULL, mensaje_id varchar(512) NOT NULL,
                creado_en timestamptz NOT NULL DEFAULT now(), actualizado_en timestamptz NOT NULL DEFAULT now(),
                UNIQUE (phone_number_id, mensaje_id),
                CHECK ((modalidad = 'contraentrega' AND pago_envio = 'no_aplica')
                    OR (modalidad = 'olva' AND pago_envio = 'anticipado')
                    OR (modalidad = 'shalom' AND pago_envio = 'destino'))
            )""")
            cur.execute("CREATE INDEX IF NOT EXISTS solicitud_entrega_contacto_idx ON whatsapp.solicitud_entrega(contacto_id, creado_en DESC)")


def fecha_contraentrega(momento=None):
    momento = momento or datetime.now(LIMA)
    if momento.tzinfo is None:
        raise ValueError("La fecha debe incluir zona horaria")
    momento = momento.astimezone(LIMA)
    sabado = momento.date() + timedelta(days=(5 - momento.weekday()) % 7)
    corte = datetime.combine(sabado - timedelta(days=1), time(17), tzinfo=LIMA)
    if momento > corte:
        sabado += timedelta(days=7)
    return sabado


def datos_envio(cur, contacto_id):
    cur.execute("""SELECT d.* FROM cliente.destinatario_envio d
        JOIN whatsapp.contacto c ON c.id = %s
        LEFT JOIN seguridad.usuario u ON u.id = c.usuario_id
        WHERE d.activo = true AND (d.contacto_whatsapp_id = c.id OR
            (d.usuario_id = c.usuario_id AND u.whatsapp_verificado = true AND u.whatsapp = c.numero))
        ORDER BY d.predeterminado DESC, d.creado_en DESC""", (contacto_id,))
    return cur.fetchall()


def crear_solicitud(cur, contacto_id, modalidad, phone_id, mensaje_id, momento=None):
    if modalidad not in PAGO_ENVIO:
        raise ValueError("Modalidad de entrega invalida")
    destinos = datos_envio(cur, contacto_id) if modalidad != "contraentrega" else []
    # Selecting the carrier does not confirm an old address without the customer's approval.
    estado = "pendiente_datos" if modalidad != "contraentrega" else "pendiente"
    fecha = fecha_contraentrega(momento) if modalidad == "contraentrega" else None
    cur.execute("""INSERT INTO whatsapp.solicitud_entrega
        (id, contacto_id, modalidad, pago_envio, estado, fecha_entrega, phone_number_id, mensaje_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (phone_number_id, mensaje_id) DO NOTHING RETURNING id""",
        (str(uuid4()), contacto_id, modalidad, PAGO_ENVIO[modalidad], estado, fecha, phone_id, mensaje_id))
    creada = cur.fetchone()
    return {"creada": bool(creada), "solicitudId": str(creada["id"]) if creada else None,
            "estado": estado, "fechaEntrega": fecha.isoformat() if fecha else None,
            "pagoEnvio": PAGO_ENVIO[modalidad], "destinatarios": destinos}
