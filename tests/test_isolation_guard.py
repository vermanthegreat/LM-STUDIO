"""The test-session isolation guard itself (see tests/conftest.py)."""

from __future__ import annotations

import os
import socket
import sqlite3

import pytest

import db
from tests import conftest


def test_default_database_path_is_refused():
    with pytest.raises(conftest.IsolationViolation):
        sqlite3.connect(conftest.DEFAULT_DB_PATH)
    assert not conftest.DEFAULT_DB_PATH.exists()


def test_db_module_default_argument_is_refused():
    # db.DB_PATH is bound as a default argument at import time; the guard must
    # still catch it without relying on monkeypatching the module constant.
    with pytest.raises(conftest.IsolationViolation):
        db.list_leads()


def test_uri_form_of_default_database_is_refused():
    with pytest.raises(conftest.IsolationViolation):
        sqlite3.connect(f"file:{conftest.DEFAULT_DB_PATH}?mode=ro", uri=True)


def test_temporary_and_memory_databases_are_allowed(tmp_path):
    sqlite3.connect(":memory:").close()
    path = tmp_path / "scratch.db"
    db.init_db(path)
    assert db.list_leads(path) == []


def test_environment_is_pinned_to_safe_values():
    assert os.environ["DATABASE_URL"] == ""
    assert os.environ["GMAIL_ENABLED"] == "false"
    assert os.environ["GMAIL_TOKEN_PATH"] == ""
    assert os.environ["GMAIL_CLIENT_SECRET_PATH"] == ""
    assert os.environ["KNOWLEDGE_EMBEDDING_ALLOW_REMOTE"] == "false"
    assert not os.environ["DATABASE_PATH"].endswith(os.sep + "leads.db")


def test_dotenv_cannot_override_pinned_values(tmp_path):
    from dotenv import load_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=postgresql://user:pw@prod/contacts\nGMAIL_ENABLED=true\n",
        encoding="utf-8",
    )
    load_dotenv(env_file)
    assert os.environ["DATABASE_URL"] == ""
    assert os.environ["GMAIL_ENABLED"] == "false"


@pytest.mark.parametrize("address", [("127.0.0.1", 1234), ("localhost", 8025), ("203.0.113.5", 443)])
def test_outbound_network_connections_are_refused(address):
    family = socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        with pytest.raises(conftest.IsolationViolation):
            sock.connect(address)
        with pytest.raises(conftest.IsolationViolation):
            sock.connect_ex(address)


def test_non_disposable_test_database_url_is_rejected():
    with pytest.raises(pytest.UsageError):
        conftest._disposable_test_database_url("postgresql://u:p@localhost:5432/contacts")
    assert conftest._disposable_test_database_url("") == ""
    url = "postgresql://u:p@localhost:5432/contacts_test"
    assert conftest._disposable_test_database_url(url) == url
