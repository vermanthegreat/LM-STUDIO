"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).parent


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return int(raw)


@dataclass(frozen=True)
class AppConfig:
    app_host: str = "127.0.0.1"
    port: int = 8025
    database_path: Path = BASE_DIR / "leads.db"
    database_url: str | None = None
    max_paste_chars: int = 200_000
    lmstudio_base_url: str = "http://localhost:1234/v1"
    lmstudio_model: str = "local-model"
    lmstudio_timeout: float = 60.0
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> AppConfig:
        db_path = os.getenv("DATABASE_PATH", "").strip()
        database_url = os.getenv("DATABASE_URL", "").strip() or None
        return cls(
            app_host=os.getenv("APP_HOST", "127.0.0.1").strip() or "127.0.0.1",
            port=_env_int("PORT", 8025),
            database_path=Path(db_path) if db_path else BASE_DIR / "leads.db",
            database_url=database_url,
            max_paste_chars=_env_int("MAX_PASTE_CHARS", 200_000),
            lmstudio_base_url=os.getenv("LMSTUDIO_BASE_URL", "http://localhost:1234/v1").strip()
            or "http://localhost:1234/v1",
            lmstudio_model=os.getenv("LMSTUDIO_MODEL", "local-model").strip() or "local-model",
            lmstudio_timeout=float(os.getenv("LMSTUDIO_TIMEOUT", "60") or "60"),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        )
