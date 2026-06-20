"""Contact store factory."""

from __future__ import annotations

from config import AppConfig
from repositories.postgres_store import PostgresContactStore
from repositories.sqlite_store import SqliteContactStore


def get_contact_store(config: AppConfig):
    if config.database_url:
        return PostgresContactStore(config.database_url)
    return SqliteContactStore(config.database_path)
