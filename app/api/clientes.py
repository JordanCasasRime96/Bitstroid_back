import re
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException
from app.core.security import verificar_jwt
from app.core.normalizacion import _normalizar_whatsapp
from app.db.compat import connect, dict_row
from app.db.session import connection_kwargs
from app.db.schema import _ensure_pago_tables
from app.schemas.clientes import SolicitudEnvioPayload, DestinatarioEnvioPayload

router = APIRouter()


@router.get("/api/cliente/almacen")
def listar_almacen_cliente(usuario=Depends(verificar_jwt)):
    _ensure_pago_tables()
    with connect(**connection_kwargs(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, total, items, creado_en, validado_en, envio_tipo, envio_solicitado_en, destinatario_id
                FROM publicacion.solicitud_pago
                WHERE usuario_id = %s AND estado = 'validado'
                ORDER BY validado_en DESC NULLS LAST, creado_en DESC
                """,
                (usuario["sub"],),
            )
            rows = cur.fetchall()
    return [{
        "id": str(row["id"]), "total": float(row["total"] or 0), "items": row["items"] or [],
        "creadoEn": row["creado_en"].isoformat(),
        "validadoEn": row["validado_en"].isoformat() if row["validado_en"] else None,
        "envioTipo": row["envio_tipo"],
        "destinatarioId": str(row["destinatario_id"]) if row["destinatario_id"] else None,
        "envioSolicitadoEn": row["envio_solicitado_en"].isoformat() if row["envio_solicitado_en"] else None,
} for row in rows]



@router.get("/api/cliente/destinatarios")
def listar_destinatarios_cliente(usuario=Depends(verificar_jwt)):
    _ensure_pago_tables()
    with connect(**connection_kwargs(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, nombre_completo, dni, direccion, referencia, provincia, distrito, contacto, predeterminado
                FROM cliente.destinatario_envio
                WHERE usuario_id = %s AND activo = true
                ORDER BY predeterminado DESC, creado_en DESC
                """,
                (usuario["sub"],),
            )
            rows = cur.fetchall()
    return [{
        "id": str(row["id"]), "nombreCompleto": row["nombre_completo"], "dni": row["dni"],
        "direccion": row["direccion"], "referencia": row["referencia"], "provincia": row["provincia"],
        "distrito": row["distrito"], "contacto": row["contacto"], "predeterminado": row["predeterminado"],
    } for row in rows]



@router.post("/api/cliente/destinatarios")
def crear_destinatario_cliente(payload: DestinatarioEnvioPayload, usuario=Depends(verificar_jwt)):
    _ensure_pago_tables()
    nombre = payload.nombreCompleto.strip()
    dni = re.sub(r"\D+", "", payload.dni)
    direccion = payload.direccion.strip()
    provincia = payload.provincia.strip()
    distrito = payload.distrito.strip()
    contacto = _normalizar_whatsapp(payload.contacto)
    if not nombre or not direccion or not provincia or not distrito or not 8 <= len(dni) <= 12:
        raise HTTPException(status_code=400, detail="Completa correctamente los datos del destinatario")
    destinatario_id = str(uuid4())
    with connect(**connection_kwargs(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM cliente.destinatario_envio WHERE usuario_id = %s AND activo = true LIMIT 1", (usuario["sub"],))
            predeterminado = payload.predeterminado or not bool(cur.fetchone())
            if predeterminado:
                cur.execute("UPDATE cliente.destinatario_envio SET predeterminado = false WHERE usuario_id = %s", (usuario["sub"],))
            cur.execute(
                """
                INSERT INTO cliente.destinatario_envio (
                    id, usuario_id, nombre_completo, dni, direccion, referencia, provincia, distrito, contacto, predeterminado
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (destinatario_id, usuario["sub"], nombre, dni, direccion, payload.referencia.strip(), provincia, distrito, contacto, predeterminado),
            )
            conn.commit()
    return {"ok": True, "id": destinatario_id, "predeterminado": predeterminado}



@router.post("/api/cliente/almacen/{pago_id}/solicitar-envio")
def solicitar_envio_almacen(pago_id: str, payload: SolicitudEnvioPayload, usuario=Depends(verificar_jwt)):
    _ensure_pago_tables()
    if payload.tipo not in {"contra_entrega", "courrier"}:
        raise HTTPException(status_code=400, detail="Tipo de envío inválido")
    with connect(**connection_kwargs(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            destinatario_id = None
            if payload.tipo == "courrier":
                if not payload.destinatarioId:
                    raise HTTPException(status_code=400, detail="Selecciona un destinatario")
                cur.execute("SELECT id FROM cliente.destinatario_envio WHERE id = %s AND usuario_id = %s AND activo = true", (payload.destinatarioId, usuario["sub"]))
                destinatario = cur.fetchone()
                if not destinatario:
                    raise HTTPException(status_code=404, detail="Destinatario no encontrado")
                destinatario_id = destinatario["id"]
            cur.execute(
                """
                UPDATE publicacion.solicitud_pago
                SET envio_tipo = %s, destinatario_id = %s, envio_solicitado_en = now()
                WHERE id = %s AND usuario_id = %s AND estado = 'validado'
                RETURNING id, envio_solicitado_en
                """,
                (payload.tipo, destinatario_id, pago_id, usuario["sub"]),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Compra no encontrada")
            conn.commit()
    return {"ok": True, "envioTipo": payload.tipo, "envioSolicitadoEn": row["envio_solicitado_en"].isoformat()}
