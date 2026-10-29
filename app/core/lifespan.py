from contextlib import asynccontextmanager, suppress
import asyncio
import logging
from os import getenv
from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool
from app.db.session import connection_kwargs
from app.services.whatsapp.registro import configuracion as configuracion_whatsapp, preparar_tabla as preparar_registro_whatsapp
from app.services.whatsapp.respuestas import despachar as despachar_respuestas, diagnostico_db
from app.services.whatsapp import bot as whatsapp_bot


@asynccontextmanager
async def lifespan(app):
    logger = logging.getLogger("uvicorn.error")
    if not getenv("WHATSAPP_ACCESS_TOKEN", "").strip():
        logger.warning("WhatsApp: WHATSAPP_ACCESS_TOKEN ausente; las respuestas automaticas estan desactivadas")
    else:
        logger.info("WhatsApp: worker de respuestas automaticas iniciado")
    logger.info("WhatsApp: bot interactivo %s", "habilitado; requiere smb_message_echoes para pausa humana" if whatsapp_bot.habilitado() else "desactivado (WHATSAPP_BOT_ENABLED)")
    try:
        configuracion_whatsapp()
    except HTTPException:
        logger.warning("WhatsApp: configuracion del webhook incompleta; revisar APP_SECRET, VERIFY_TOKEN, OTP_SECRET y PHONE_NUMBER_ID")
    async def respuestas():
        preparado = False
        while True:
            if getenv("WHATSAPP_ACCESS_TOKEN", "").strip():
                try:
                    if not preparado:
                        await run_in_threadpool(preparar_registro_whatsapp, tuple(sorted(connection_kwargs().items())))
                        preparado = True
                    await run_in_threadpool(despachar_respuestas, connection_kwargs())
                    await run_in_threadpool(whatsapp_bot.despachar, connection_kwargs())
                except Exception as error:
                    logger.warning("Cola WhatsApp no disponible: %s", diagnostico_db(error))
                    await asyncio.sleep(28)
            await asyncio.sleep(2)
    task = asyncio.create_task(respuestas())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
