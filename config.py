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


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


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
    gmail_enabled: bool = False
    gmail_client_secret_path: Path | None = None
    gmail_token_path: Path | None = None
    gmail_sync_label: str = "LMStudio"
    gmail_sync_limit: int = 100
    app_timezone: str = "Asia/Jerusalem"
    knowledge_storage_dir: Path = BASE_DIR / "knowledge_store"
    knowledge_max_upload_bytes: int = 25 * 1024 * 1024
    knowledge_vision_model: str | None = None
    knowledge_classify: bool = True

    @classmethod
    def from_env(cls) -> AppConfig:
        db_path = os.getenv("DATABASE_PATH", "").strip()
        database_url = os.getenv("DATABASE_URL", "").strip() or None
        secret_path = os.getenv("GMAIL_CLIENT_SECRET_PATH", "").strip()
        token_path = os.getenv("GMAIL_TOKEN_PATH", "").strip()
        knowledge_dir = os.getenv("KNOWLEDGE_STORAGE_DIR", "").strip()
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
            gmail_enabled=_env_bool("GMAIL_ENABLED", False),
            gmail_client_secret_path=Path(secret_path) if secret_path else None,
            gmail_token_path=Path(token_path) if token_path else None,
            gmail_sync_label=os.getenv("GMAIL_SYNC_LABEL", "LMStudio").strip() or "LMStudio",
            gmail_sync_limit=_env_int("GMAIL_SYNC_LIMIT", 100),
            app_timezone=os.getenv("APP_TIMEZONE", "Asia/Jerusalem").strip() or "Asia/Jerusalem",
            knowledge_storage_dir=Path(knowledge_dir) if knowledge_dir else BASE_DIR / "knowledge_store",
            knowledge_max_upload_bytes=_env_int("KNOWLEDGE_MAX_UPLOAD_MB", 25) * 1024 * 1024,
            knowledge_vision_model=os.getenv("KNOWLEDGE_VISION_MODEL", "").strip() or None,
            knowledge_classify=_env_bool("KNOWLEDGE_CLASSIFY", True),
        )
