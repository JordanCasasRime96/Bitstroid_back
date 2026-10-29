"""Compatibility entry point for existing Docker and uvicorn commands."""
from app.main import app

__all__ = ["app"]
