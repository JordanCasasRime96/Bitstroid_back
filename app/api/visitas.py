from fastapi import APIRouter, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool
from app.db.session import connection_kwargs
from app.services import visitas

router = APIRouter()


@router.post("/api/publico/visitas", status_code=204)
async def registrar_visita(request: Request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 2048:
            raise HTTPException(413, 'Solicitud demasiado grande')
    datos = visitas.validar(bytes(body), request.headers.get('x-visita-tiempo', ''),
                            request.headers.get('x-visita-firma', ''))
    await run_in_threadpool(visitas.guardar, connection_kwargs(), datos)
    return Response(status_code=204)
