"""Test-session isolation guard.

Automated tests must never reach the operator's real SQLite database, the
production PostgreSQL database, OAuth credentials, LM Studio, Gmail or any other
network service. This module runs before any test module is imported, so it:

1. Pins configuration environment variables to safe values *before* modules
   such as ``app`` call ``load_dotenv()`` (which never overrides existing keys),
   so a local ``.env`` cannot inject real paths, credentials or database URLs.
2. Refuses ``sqlite3`` connections to the default repository ``leads.db`` and to
   any ``DATABASE_PATH`` configured in the invoking shell. Default function
   arguments bound to ``db.DB_PATH`` are therefore caught at connect time.
3. Refuses outbound INET/INET6 socket connections. The only exception is the
   host/port of an explicitly supplied disposable ``TEST_DATABASE_URL``.

``TEST_DATABASE_URL`` is honoured only when it is set in the invoking process
environment (never from ``.env``) and names a database whose name contains
``test``. Otherwise it is blanked and PostgreSQL integration tests skip.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = REPO_ROOT / "leads.db"

_SESSION_DIR = Path(tempfile.mkdtemp(prefix="lmstudio-tests-"))


def _protected_db_paths() -> set[Path]:
    paths = {DEFAULT_DB_PATH.resolve()}
    configured = os.environ.get("DATABASE_PATH", "").strip()
    if configured:
        paths.add(Path(configured).expanduser().resolve())
    return paths


PROTECTED_DB_PATHS = frozenset(_protected_db_paths())


def _disposable_test_database_url(raw: str | None) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    name = urlsplit(raw).path.lstrip("/")
    if "test" not in name.lower():
        raise pytest.UsageError(
            "TEST_DATABASE_URL must name a disposable test database "
            f"(database name must contain 'test'; got {name!r})."
        )
    return raw


TEST_DATABASE_URL = _disposable_test_database_url(os.environ.get("TEST_DATABASE_URL"))

_SAFE_ENV = {
    "DATABASE_PATH": str(_SESSION_DIR / "default-unused.db"),
    "DATABASE_URL": "",
    "TEST_DATABASE_URL": TEST_DATABASE_URL,
    "GMAIL_ENABLED": "false",
    "GMAIL_CLIENT_SECRET_PATH": "",
    "GMAIL_TOKEN_PATH": "",
    "KNOWLEDGE_STORAGE_DIR": str(_SESSION_DIR / "knowledge_store"),
    "KNOWLEDGE_EMBEDDINGS_ENABLED": "false",
    "KNOWLEDGE_EMBEDDING_ALLOW_REMOTE": "false",
    "LMSTUDIO_API_KEY": "",
}
os.environ.update(_SAFE_ENV)


class IsolationViolation(RuntimeError):
    """Raised when a test tries to reach real data or an external service."""


def _sqlite_target(database: object) -> Path | None:
    if isinstance(database, bytes):
        database = database.decode()
    if not isinstance(database, (str, os.PathLike)):
        return None
    text = os.fspath(database)
    if text == ":memory:" or text == "":
        return None
    if text.startswith("file:"):
        text = text[5:].split("?", 1)[0]
        if not text or text == ":memory:":
            return None
    return Path(text).expanduser().resolve()


_real_sqlite_connect = sqlite3.connect


def _guarded_sqlite_connect(database, *args, **kwargs):
    target = _sqlite_target(database)
    if target is not None and target in PROTECTED_DB_PATHS:
        raise IsolationViolation(
            f"Test attempted to open a protected database: {target}. "
            "Pass an explicit tmp_path database instead."
        )
    return _real_sqlite_connect(database, *args, **kwargs)


sqlite3.connect = _guarded_sqlite_connect
sqlite3.dbapi2.connect = _guarded_sqlite_connect


def _allowed_endpoints() -> set[tuple[str, int]]:
    if not TEST_DATABASE_URL:
        return set()
    parts = urlsplit(TEST_DATABASE_URL)
    return {((parts.hostname or "localhost").lower(), parts.port or 5432)}


_ALLOWED_ENDPOINTS = frozenset(_allowed_endpoints())
_LOOPBACK_ALIASES = {"localhost", "127.0.0.1", "::1"}
_real_socket_connect = socket.socket.connect
_real_socket_connect_ex = socket.socket.connect_ex


def _endpoint_allowed(address: object) -> bool:
    if not isinstance(address, tuple) or len(address) < 2:
        return False
    host, port = str(address[0]).lower(), int(address[1])
    for allowed_host, allowed_port in _ALLOWED_ENDPOINTS:
        if port != allowed_port:
            continue
        if host == allowed_host or (host in _LOOPBACK_ALIASES and allowed_host in _LOOPBACK_ALIASES):
            return True
    return False


def _check_socket(sock: socket.socket, address: object) -> None:
    if sock.family in (socket.AF_INET, socket.AF_INET6) and not _endpoint_allowed(address):
        raise IsolationViolation(
            f"Test attempted a network connection to {address!r}. "
            "Use a fake provider or transport instead."
        )


def _guarded_connect(self, address):
    _check_socket(self, address)
    return _real_socket_connect(self, address)


def _guarded_connect_ex(self, address):
    _check_socket(self, address)
    return _real_socket_connect_ex(self, address)


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex


@pytest.fixture(scope="session", autouse=True)
def _default_database_untouched():
    """Fail the session if the default repository database was created or changed."""
    before = DEFAULT_DB_PATH.stat() if DEFAULT_DB_PATH.exists() else None
    yield
    after = DEFAULT_DB_PATH.stat() if DEFAULT_DB_PATH.exists() else None
    if before is None:
        assert after is None, f"Test session created {DEFAULT_DB_PATH}"
    else:
        assert after is not None
        assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
