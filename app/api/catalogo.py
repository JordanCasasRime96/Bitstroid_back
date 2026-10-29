import asyncio
import json
from typing import Literal
from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from app.services import catalogo
from app.services.catalogo import _ultima_publicacion_publica

router = APIRouter()


@router.get("/api/publico/catalogo")
def listar_catalogo_publico(
    paginado: bool = False, pagina: int = Query(default=1, ge=1), limite: int = Query(default=20, ge=1, le=20),
    orden: Literal['reciente', 'antiguo', 'menor-precio', 'mayor-precio', 'visitados'] = 'reciente',
    busqueda: str = '', plataforma: str = '', ids: str = '',
):
    return catalogo.listar_catalogo_publico(paginado, pagina, limite, orden, busqueda, plataforma, ids)


@router.get('/api/publico/publicaciones/{publicacion_id}')
def detalle_publicacion_publica(publicacion_id: str):
    return catalogo.detalle_publicacion_publica(publicacion_id)


@router.get("/api/publico/catalogo/eventos")
async def eventos_catalogo_publico(since: str = ""):
    async def stream():
        ultima_fecha = since
        while True:
            try:
                ultima = _ultima_publicacion_publica()
                fecha = ultima["fechaCreacion"] if ultima else ""
                if ultima and fecha and fecha != ultima_fecha:
                    ultima_fecha = fecha
                    payload = json.dumps(ultima, ensure_ascii=False)
                    yield f"event: nueva-publicacion\ndata: {payload}\n\n"
                else:
                    yield ": ping\n\n"
            except Exception:
                yield ": error\n\n"
            await asyncio.sleep(6)

    return StreamingResponse(stream(), media_type="text/event-stream")
