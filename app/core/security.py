from datetime import datetime, timedelta, timezone
from os import getenv
from typing import Any
import jwt
from fastapi import Header, HTTPException

JWT_FALLBACK_SECRET = "bitstroid-local-dev-secret-32-bytes-minimo"


def _crear_token(usuario: dict[str, Any]) -> str:
    secret = getenv("JWT_SECRET") or getenv("SECRET_KEY") or JWT_FALLBACK_SECRET
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(usuario["id"]),
        "apodo": usuario["apodo"],
        "rol": "cliente",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=4)).timestamp()),
    }
    return jwt.encode(payload, secret, algorithm="HS256")



def verificar_jwt(authorization: str = Header(default="")) -> dict[str, Any]:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token requerido")
    token = authorization.removeprefix("Bearer ").strip()
    secret = getenv("JWT_SECRET") or getenv("SECRET_KEY") or JWT_FALLBACK_SECRET
    try:
        return jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=401, detail="Sesion expirada") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="Token invalido") from exc



def _jwt_opcional(authorization: str = Header(default="")) -> dict[str, Any] | None:
    if not authorization.startswith("Bearer "):
        return None
    try:
        return verificar_jwt(authorization)
    except HTTPException:
        return None
