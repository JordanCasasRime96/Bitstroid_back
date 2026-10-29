from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException
from app.core.security import verificar_jwt, _jwt_opcional
from app.db.compat import connect, dict_row
from app.db.session import connection_kwargs
from app.db.schema import _ensure_cliente_interacciones
from app.schemas.tienda import InteraccionPublicacionPayload

router = APIRouter()


@router.post("/api/interacciones/publicacion")
def registrar_interaccion_publicacion(payload: InteraccionPublicacionPayload, usuario=Depends(_jwt_opcional)):
    if not usuario:
        return {"ok": True, "registrado": False}
    publicacion_id = payload.publicacionId.strip()
    if not publicacion_id:
        raise HTTPException(status_code=400, detail="Publicacion requerida")
    _ensure_cliente_interacciones()
    categoria = payload.categoria.strip()[:160]
    evento = (payload.evento.strip() or "click")[:40]
    with connect(**connection_kwargs(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cliente.publicacion_interaccion (id, usuario_id, publicacion_id, categoria, evento)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (str(uuid4()), usuario["sub"], publicacion_id, categoria, evento),
            )
    return {"ok": True, "registrado": True}



@router.get("/api/interacciones/preferencias")
def obtener_preferencias_publicacion(usuario=Depends(verificar_jwt)):
    _ensure_cliente_interacciones()
    with connect(**connection_kwargs(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT categoria, count(*) AS total
                FROM cliente.publicacion_interaccion
                WHERE usuario_id = %s
                  AND btrim(categoria) <> ''
                  AND creado_en >= now() - interval '90 days'
                GROUP BY categoria
                ORDER BY total DESC, max(creado_en) DESC
                LIMIT 5
                """,
                (usuario["sub"],),
            )
            categorias = cur.fetchall()
            cur.execute(
                """
                SELECT publicacion_id, max(creado_en) AS ultima_visita, count(*) AS total
                FROM cliente.publicacion_interaccion
                WHERE usuario_id = %s
                GROUP BY publicacion_id
                ORDER BY ultima_visita DESC
                LIMIT 12
                """,
                (usuario["sub"],),
            )
            publicaciones = cur.fetchall()
    return {
        "categorias": [{"categoria": row["categoria"], "total": int(row["total"] or 0)} for row in categorias],
        "publicaciones": [
            {
                "publicacionId": str(row["publicacion_id"]),
                "total": int(row["total"] or 0),
                "ultimaVisita": row["ultima_visita"].isoformat() if row["ultima_visita"] else "",
            }
            for row in publicaciones
        ],
    }
