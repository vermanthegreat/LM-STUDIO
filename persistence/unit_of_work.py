"""Unit of work coordinating repository access within one transaction."""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from persistence.session import get_session_factory


class UnitOfWork:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self._session: Optional[Session] = None

    @property
    def session(self) -> Session:
        if self._session is None:
            raise RuntimeError("UnitOfWork is not active")
        return self._session

    def __enter__(self) -> "UnitOfWork":
        factory = get_session_factory(self.database_url)
        self._session = factory()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._session is None:
            return
        try:
            if exc_type is None:
                self._session.commit()
            else:
                self._session.rollback()
        finally:
            self._session.close()
            self._session = None
