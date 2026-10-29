from app.db.compat import connect, dict_row
from app.db.session import connection_kwargs


def _ensure_cliente_interacciones() -> None:
    with connect(**connection_kwargs(), row_factory=dict_row) as conn:
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
    with connect(**connection_kwargs(), row_factory=dict_row) as conn:
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
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS publicacion.solicitud_carrito (
                    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                    usuario_id uuid REFERENCES seguridad.usuario(id) ON DELETE SET NULL,
                    invitado_nombre text NOT NULL DEFAULT '',
                    invitado_whatsapp text NOT NULL DEFAULT '',
                    items jsonb NOT NULL DEFAULT '[]'::jsonb,
                    total_pen numeric(14, 2) NOT NULL DEFAULT 0,
                    total_usd numeric(14, 2) NOT NULL DEFAULT 0,
                    creado_en timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS solicitud_carrito_creado_en_idx ON publicacion.solicitud_carrito(creado_en DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS solicitud_carrito_usuario_idx ON publicacion.solicitud_carrito(usuario_id, creado_en DESC)")
            conn.commit()
