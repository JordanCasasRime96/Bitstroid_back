import base64
import binascii
import json
from pathlib import Path
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException
from app.core.normalizacion import _normalizar_whatsapp
from app.core.runtime import image_storage_dir
from app.core.security import _jwt_opcional
from app.db.compat import connect, dict_row
from app.db.session import connection_kwargs
from app.db.schema import _ensure_pago_tables
from app.schemas.tienda import ConfirmacionPagoPayload, SolicitudCarritoPayload

router = APIRouter()


@router.get("/api/publico/configuracion-pago")
def obtener_configuracion_pago_publica():
    _ensure_pago_tables()
    with connect(**connection_kwargs(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT contacto_celular, qr_ruta FROM publicacion.configuracion_pago WHERE id = 1")
            row = cur.fetchone() or {}
    return {"contactoCelular": row.get("contacto_celular") or "", "qrRuta": row.get("qr_ruta") or ""}



@router.post("/api/publico/pagos/confirmar")
def confirmar_pago(payload: ConfirmacionPagoPayload, usuario=Depends(_jwt_opcional)):
    _ensure_pago_tables()
    if not payload.items:
        raise HTTPException(status_code=400, detail="No hay productos para confirmar")
    invitado_nombre = payload.invitadoNombre.strip()
    invitado_whatsapp = payload.invitadoWhatsapp.strip()
    if not usuario:
        if not invitado_nombre:
            raise HTTPException(status_code=400, detail="Ingresa tu nombre para continuar como invitado")
        invitado_whatsapp = _normalizar_whatsapp(invitado_whatsapp)
    if not payload.comprobanteMimeType.startswith("image/"):
        raise HTTPException(status_code=400, detail="El comprobante debe ser una imagen")
    try:
        contenido = payload.comprobanteContenidoBase64.split(",", 1)[-1]
        data = base64.b64decode(contenido, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Comprobante inválido") from exc
    if not data or len(data) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="El comprobante debe pesar menos de 5 MB")

    extension = Path(payload.comprobanteNombre).suffix.lower()
    if extension not in {".jpg", ".jpeg", ".png", ".webp"}:
        extension = ".jpg"
    solicitud_id = str(uuid4())
    carpeta = image_storage_dir / "pagos" / "comprobantes"
    carpeta.mkdir(parents=True, exist_ok=True)
    nombre = f"{solicitud_id}{extension}"
    (carpeta / nombre).write_bytes(data)
    ruta = f"/uploads/pagos/comprobantes/{nombre}"

    with connect(**connection_kwargs(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO publicacion.solicitud_pago (
                    id, usuario_id, total, items, comprobante_ruta, invitado_nombre, invitado_whatsapp
                ) VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)
                """,
                (solicitud_id, usuario.get("sub") if usuario else None, payload.total, json.dumps(payload.items, ensure_ascii=False), ruta, invitado_nombre, invitado_whatsapp),
            )
            conn.commit()
    return {"ok": True, "solicitudId": solicitud_id, "estado": "pendiente"}



@router.post("/api/publico/solicitud-carrito")
def registrar_solicitud_carrito(payload: SolicitudCarritoPayload, usuario=Depends(_jwt_opcional)):
    _ensure_pago_tables()
    items = [item for item in payload.items if isinstance(item, dict)]
    if not items:
        raise HTTPException(status_code=400, detail="El carrito esta vacio")
    total_pen = round(sum(float(item.get("precio", 0) or 0) * int(item.get("cantidad", 1) or 1) for item in items if item.get("moneda") == "PEN"), 2)
    total_usd = round(sum(float(item.get("precio", 0) or 0) * int(item.get("cantidad", 1) or 1) for item in items if item.get("moneda") == "USD"), 2)
    solicitud_id = str(uuid4())
    with connect(**connection_kwargs(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO publicacion.solicitud_carrito (
                    id, usuario_id, invitado_nombre, invitado_whatsapp, items, total_pen, total_usd
                ) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
                """,
                (solicitud_id, usuario.get("sub") if usuario else None, payload.invitadoNombre.strip()[:160], payload.invitadoWhatsapp.strip()[:30], json.dumps(items, ensure_ascii=False), total_pen, total_usd),
            )
            conn.commit()
    return {"ok": True, "solicitudId": solicitud_id}
