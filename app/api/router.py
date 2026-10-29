from fastapi import APIRouter
from app.api import auth, catalogo, clientes, estado, interacciones, pagos, visitas, webhooks

router = APIRouter()
for modulo in (estado, auth, webhooks, interacciones, pagos, clientes, visitas, catalogo):
    router.include_router(modulo.router)
