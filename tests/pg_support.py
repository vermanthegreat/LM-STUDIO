"""Shared helpers for disposable PostgreSQL integration tests."""

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from persistence.session import get_engine, reset_cached_engines
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]


def alembic_config(database_url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    os.environ["DATABASE_URL"] = database_url
    return cfg


def reset_public_schema(database_url: str) -> None:
    engine = get_engine(database_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    reset_cached_engines()


def run_alembic_upgrade(database_url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(database_url), revision)
