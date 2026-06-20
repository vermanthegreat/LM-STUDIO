"""Database engine and session factory."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Generator, Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from persistence.models import Base

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


def init_schema(database_url: str) -> None:
    engine = get_engine(database_url)
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
