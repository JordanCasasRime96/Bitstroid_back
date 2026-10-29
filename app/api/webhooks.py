import asyncio
import json
import logging
from fastapi import APIRouter, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool
from app.db.session import connection_kwargs
from app.services.whatsapp.registro import (configuracion as configuracion_whatsapp,
    verificar_firma, verificar_webhook, procesar_mensajes)
from app.services.whatsapp.contactos import mensajes_entrantes
from app.services.whatsapp import bot as whatsapp_bot

router = APIRouter()


@router.get("/api/webhooks/whatsapp")
def confirmar_webhook(request: Request):
    params = request.query_params
    challenge = verificar_webhook(params.get("hub.mode"), params.get("hub.verify_token"), params.get("hub.challenge"))
    return Response(content=challenge, media_type="text/plain", headers={"Cache-Control": "no-store"})



@router.post("/api/webhooks/whatsapp")
async def recibir_whatsapp(request: Request):
    app_secret = configuracion_whatsapp()[1]
    async def leer_body():
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 65536:
                raise HTTPException(413, "Webhook demasiado grande")
            body.extend(chunk)
        return bytes(body)
    try:
        body = await asyncio.wait_for(leer_body(), timeout=5)
    except TimeoutError:
        raise HTTPException(408, "Tiempo de espera agotado") from None
    try:
        verificar_firma(body, request.headers.get("x-hub-signature-256"), app_secret)
    except HTTPException:
        logging.getLogger("uvicorn.error").warning("Webhook WhatsApp rechazado: firma invalida. Revisar APP_SECRET y cabecera X-Hub-Signature-256 del proxy")
        raise
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise HTTPException(400, "Webhook invalido") from None
    logging.getLogger("uvicorn.error").info("Webhook WhatsApp: POST firmado recibido")
    await run_in_threadpool(procesar_mensajes, connection_kwargs(), payload)
    phone_id = configuracion_whatsapp()[3]
    await run_in_threadpool(whatsapp_bot.procesar, connection_kwargs(), payload,
                           list(mensajes_entrantes(payload, phone_id)), phone_id)
    return {"ok": True}
