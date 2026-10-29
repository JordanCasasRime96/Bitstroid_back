from typing import Any
from app.core.runtime import settings

def connection_kwargs() -> dict[str, Any]:
    db = settings.database
    return {
        "host": db.host,
        "port": db.port,
        "dbname": db.database,
        "user": db.username,
        "password": db.password,
        "connect_timeout": 8,
    }
