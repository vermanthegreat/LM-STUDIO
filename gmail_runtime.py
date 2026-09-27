"""Gmail G0 runtime capability checks."""

from __future__ import annotations

from typing import Any


class GmailRuntimeUnsupportedError(Exception):
    """Raised when Gmail G0 operations are requested on an unsupported database runtime."""

    error_code = "gmail_postgresql_runtime_unsupported"

    def __init__(self, message: str | None = None) -> None:
        self.message = message or (
            "Gmail G0 intake is supported on the SQLite runtime only. "
            "PostgreSQL Gmail persistence is not implemented in this phase."
        )
        super().__init__(self.message)


def require_gmail_sqlite_runtime(store: Any) -> None:
    if getattr(store, "backend", "sqlite") != "sqlite":
        raise GmailRuntimeUnsupportedError()
