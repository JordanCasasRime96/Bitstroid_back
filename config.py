from dataclasses import dataclass
from os import getenv
from pathlib import Path
import os


def load_env(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if getenv(key) is None:
            os.environ[key] = value.strip().strip('"').strip("'")


@dataclass(frozen=True)
class DatabaseSettings:
    host: str
    port: int
    username: str
    password: str
    database: str


@dataclass(frozen=True)
class Settings:
    port: int
    database: DatabaseSettings


def get_settings() -> Settings:
    load_env()
    return Settings(
        port=int(getenv("PORT", "7200")),
        database=DatabaseSettings(
            host=getenv("DB_HOST", "localhost"),
            port=int(getenv("DB_PORT", "5432")),
            username=getenv("DB_USERNAME", "postgres"),
            password=getenv("DB_PASSWORD", ""),
            database=getenv("DB_DATABASE", "Yami"),
        ),
    )