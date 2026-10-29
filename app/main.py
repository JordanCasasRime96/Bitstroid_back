"""Application assembly. Run with uvicorn main:app or uvicorn app.main:app."""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from os import getenv
from app.api.router import router
from app.core.lifespan import lifespan
from app.core.rate_limit import SecurityMiddleware
from app.core.runtime import image_storage_dir, login_guard


async def cachear_imagenes(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/uploads/") and response.status_code == 200:
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response



def crear_app() -> FastAPI:
    application = FastAPI(title="Bitstroid API", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    application.add_middleware(SecurityMiddleware, guard=login_guard)
    cors_origins = [
        origin.strip()
        for origin in getenv(
            "CORS_ORIGINS",
            "http://localhost:3000,http://127.0.0.1:3000,http://localhost:3001,http://127.0.0.1:3001,http://localhost:3002,http://127.0.0.1:3002,http://localhost:3003,http://127.0.0.1:3003",
        ).split(",")
        if origin.strip()
    ]
    application.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Retry-After"],
    )

    application.middleware("http")(cachear_imagenes)
    image_storage_dir.mkdir(parents=True, exist_ok=True)
    application.mount("/uploads", StaticFiles(directory=str(image_storage_dir)), name="uploads")
    application.include_router(router)
    return application


app = crear_app()
