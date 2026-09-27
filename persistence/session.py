"""Database engine and session factory."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Generator, Optional

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from persistence.models import Base

_REQUIRED_POSTGRESQL_TABLES = ("organizations", "command_log")
_REQUIRED_POSTGRESQL_COLUMNS: dict[str, tuple[str, ...]] = {
    "people": ("legacy_person_id",),
}
_POSTGRESQL_MIGRATION_HINT = (
    "PostgreSQL schema is not initialized. "
    "Run migrations before using this database: "
    "DATABASE_URL=<url> alembic upgrade head"
)


class PostgreSQLSchemaError(RuntimeError):
    """Raised when PostgreSQL is used without an Alembic-initialized schema."""


def is_postgresql_database_url(database_url: str) -> bool:
    driver = database_url.split("://", 1)[0].lower()
    return driver == "postgres" or driver.startswith("postgresql")

_engines: dict[str, Engine] = {}
_session_factories: dict[str, sessionmaker[Session]] = {}


def get_engine(database_url: str, *, echo: bool = False) -> Engine:
    if database_url not in _engines:
        _engines[database_url] = create_engine(
            database_url,
            pool_pre_ping=True,
            echo=echo,
        )
    return _engines[database_url]


def get_session_factory(database_url: str) -> sessionmaker[Session]:
    if database_url not in _session_factories:
        engine = get_engine(database_url)
        _session_factories[database_url] = sessionmaker(
            bind=engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )
    return _session_factories[database_url]


def ensure_postgresql_schema(engine: Engine) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    missing_tables = [name for name in _REQUIRED_POSTGRESQL_TABLES if name not in tables]
    if missing_tables:
        raise PostgreSQLSchemaError(
            f"{_POSTGRESQL_MIGRATION_HINT} (missing tables: {', '.join(missing_tables)})"
        )

    missing_columns: list[str] = []
    for table_name, column_names in _REQUIRED_POSTGRESQL_COLUMNS.items():
        if table_name not in tables:
            missing_columns.extend(f"{table_name}.{column}" for column in column_names)
            continue
        present = {column["name"] for column in inspector.get_columns(table_name)}
        for column_name in column_names:
            if column_name not in present:
                missing_columns.append(f"{table_name}.{column_name}")

    if missing_columns:
        raise PostgreSQLSchemaError(
            f"{_POSTGRESQL_MIGRATION_HINT} (missing columns: {', '.join(missing_columns)})"
        )


def init_schema(database_url: str) -> None:
    engine = get_engine(database_url)
    if is_postgresql_database_url(database_url):
        ensure_postgresql_schema(engine)
        return
    Base.metadata.create_all(engine)


@contextmanager
def session_scope(database_url: str) -> Generator[Session, None, None]:
    factory = get_session_factory(database_url)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_cached_engines() -> None:
    _engines.clear()
    _session_factories.clear()
