from typing import Any
from app.core.security import _crear_token
from app.db.session import connection_kwargs
from app.services.perfil_foto import obtener_foto


def _usuario_response(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "apodo": row["apodo"] or "",
        "nombres": row["nombres"] or "",
        "correo": "" if str(row["correo"] or "").endswith("@bitstroid.local") else row["correo"] or "",
        "whatsapp": row["whatsapp"] or "",
        "fotoUrl": obtener_foto(connection_kwargs(), str(row["id"])),
        "rol": "cliente",
        "requiereCambioContrasena": bool(row.get("forzar_cambio_contrasena", False)),
        "accessToken": _crear_token(row),
    }
