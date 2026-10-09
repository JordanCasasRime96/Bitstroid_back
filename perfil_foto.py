import base64
import binascii
from functools import cache
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4
import warnings

from fastapi import HTTPException
from PIL import Image, ImageOps, UnidentifiedImageError

from db_compat import connect, dict_row


def comprimir_foto(contenido: str) -> bytes:
    try:
        datos = base64.b64decode(contenido, validate=True)
        if len(datos) > 256 * 1024:
            raise HTTPException(413, "La foto es demasiado grande")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(datos)) as original:
                if original.width * original.height > 16_000_000:
                    raise HTTPException(413, "La foto tiene demasiada resolución")
                foto = ImageOps.fit(ImageOps.exif_transpose(original).convert("RGB"), (192, 192))
                salida = BytesIO()
                foto.save(salida, format="JPEG", quality=70, optimize=True)
                return salida.getvalue()
    except (binascii.Error, ValueError, OSError, UnidentifiedImageError,
            Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise HTTPException(400, "Selecciona una imagen válida") from exc


@cache
def preparar_tabla(connection_values: tuple):
    with connect(**dict(connection_values), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS cliente")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cliente.foto_perfil (
                    usuario_id uuid PRIMARY KEY REFERENCES seguridad.usuario(id) ON DELETE CASCADE,
                    ruta text NOT NULL
                )
            """)
        conn.commit()


def obtener_foto(connection_kwargs: dict, usuario_id: str) -> str:
    preparar_tabla(tuple(sorted(connection_kwargs.items())))
    with connect(**connection_kwargs, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT ruta FROM cliente.foto_perfil WHERE usuario_id = %s", (usuario_id,))
            fila = cur.fetchone()
    return fila["ruta"] if fila else ""


def guardar_foto(connection_kwargs: dict, storage: Path, usuario_id: str, foto: bytes | None) -> str:
    preparar_tabla(tuple(sorted(connection_kwargs.items())))
    usuario_id = str(UUID(usuario_id))
    carpeta = storage / "perfiles"
    carpeta.mkdir(parents=True, exist_ok=True)
    archivo = carpeta / f"{usuario_id}-{uuid4()}.jpg" if foto else None
    ruta = f"/uploads/perfiles/{archivo.name}" if archivo else ""
    if archivo:
        archivo.write_bytes(foto)
    try:
        with connect(**connection_kwargs, row_factory=dict_row) as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("SELECT id FROM seguridad.usuario WHERE id = %s AND estado = 'activo' FOR UPDATE", (usuario_id,))
                    if not cur.fetchone():
                        raise HTTPException(401, "Usuario no encontrado")
                    cur.execute("SELECT ruta FROM cliente.foto_perfil WHERE usuario_id = %s", (usuario_id,))
                    anterior = cur.fetchone()
                    if archivo:
                        cur.execute("""
                            INSERT INTO cliente.foto_perfil (usuario_id, ruta) VALUES (%s, %s)
                            ON CONFLICT (usuario_id) DO UPDATE SET ruta = EXCLUDED.ruta
                        """, (usuario_id, ruta))
                    else:
                        cur.execute("DELETE FROM cliente.foto_perfil WHERE usuario_id = %s", (usuario_id,))
    except Exception:
        if archivo:
            archivo.unlink(missing_ok=True)
        raise
    if anterior:
        viejo = carpeta / Path(anterior["ruta"]).name
        if viejo.name.startswith(f"{usuario_id}-") and viejo.suffix == ".jpg":
            try:
                viejo.unlink(missing_ok=True)
            except OSError:
                pass
    return ruta
