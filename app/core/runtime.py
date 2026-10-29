"""Shared settings and rate limiter; no database connections on import."""
from os import getenv
from pathlib import Path
from app.core.config import get_settings
from app.core.rate_limit import LoginGuard

settings = get_settings()
login_guard = LoginGuard(getenv("RATE_LIMIT_STORAGE_URL", "memory://"))
image_storage_dir = Path(getenv("IMAGE_STORAGE_DIR") or getenv("UPLOAD_DIR") or "/app/uploads")
