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


# --- Windows socketpair() emulation (asyncio self-pipe / TestClient) -----------


def _windows_style_socketpair(family=socket.AF_INET, type=socket.SOCK_STREAM, proto=0):
    """CPython's socketpair() emulation used on Windows (no AF_UNIX socketpair)."""
    fallback = getattr(socket, "_fallback_socketpair", None)
    if fallback is not None:
        return fallback(family, type, proto)
    lsock = socket.socket(family, type, proto)
    try:
        lsock.bind(("127.0.0.1", 0))
        lsock.listen()
        addr, port = lsock.getsockname()[:2]
        csock = socket.socket(family, type, proto)
        csock.setblocking(False)
        try:
            csock.connect((addr, port))
        except (BlockingIOError, InterruptedError):
            pass
        csock.setblocking(True)
        ssock, _ = lsock.accept()
    finally:
        lsock.close()
    return ssock, csock


@pytest.fixture
def windows_socketpair(monkeypatch):
    monkeypatch.setattr(conftest, "_real_socketpair", _windows_style_socketpair)


def test_emulated_socketpair_loopback_is_allowed(windows_socketpair):
    a, b = socket.socketpair()
    try:
        assert a.family == socket.AF_INET
        a.sendall(b"ping")
        assert b.recv(4) == b"ping"
    finally:
        a.close()
        b.close()


def test_asyncio_event_loop_starts_with_emulated_socketpair(windows_socketpair):
    import asyncio

    loop = asyncio.SelectorEventLoop()
    try:
        assert loop.run_until_complete(asyncio.sleep(0, result="ok")) == "ok"
    finally:
        loop.close()


def test_testclient_request_works_with_emulated_socketpair(windows_socketpair, tmp_path):
    from fastapi.testclient import TestClient

    from app import create_app
    from config import AppConfig

    cfg = AppConfig(database_path=tmp_path / "routes.db", port=8025)
    with TestClient(create_app(cfg), base_url="http://127.0.0.1:8025") as client:
        assert client.get("/ask").status_code == 200


def test_socketpair_window_does_not_open_other_loopback_ports(monkeypatch):
    attempted = {}

    def pair_that_also_dials_lm_studio(*args, **kwargs):
        pair = _windows_style_socketpair()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.connect(("127.0.0.1", 1234))
            except conftest.IsolationViolation:
                attempted["refused"] = True
        return pair

    monkeypatch.setattr(conftest, "_real_socketpair", pair_that_also_dials_lm_studio)
    a, b = socket.socketpair()
    a.close()
    b.close()
    assert attempted == {"refused": True}


def test_in_process_listener_outside_socketpair_is_refused():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            with pytest.raises(conftest.IsolationViolation):
                client.connect(("127.0.0.1", port))


def test_socketpair_listener_is_forgotten_after_the_call(windows_socketpair, monkeypatch):
    endpoints = []
    real_listen = conftest._real_socket_listen

    def recording_listen(self, *args):
        result = real_listen(self, *args)
        endpoints.append(self.getsockname()[:2])
        return result

    monkeypatch.setattr(conftest, "_real_socket_listen", recording_listen)
    a, b = socket.socketpair()
    a.close()
    b.close()
    assert len(endpoints) == 1
    assert conftest._socketpair_state.listeners is None
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(conftest.IsolationViolation):
            sock.connect(endpoints[0])


def test_other_thread_cannot_use_socketpair_window(monkeypatch):
    import threading

    endpoint = {}
    listening = threading.Event()
    release = threading.Event()

    def slow_pair(*args, **kwargs):
        lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            lsock.bind(("127.0.0.1", 0))
            lsock.listen()
            endpoint["value"] = lsock.getsockname()[:2]
            listening.set()
            release.wait(5)
            csock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            csock.connect(endpoint["value"])
            ssock, _ = lsock.accept()
        finally:
            lsock.close()
        return ssock, csock

    monkeypatch.setattr(conftest, "_real_socketpair", slow_pair)
    outcome = {}

    def run_pair():
        a, b = socket.socketpair()
        outcome["paired"] = True
        a.close()
        b.close()

    worker = threading.Thread(target=run_pair)
    worker.start()
    assert listening.wait(5)
    try:
        # The listener is live and registered for the worker thread only.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            with pytest.raises(conftest.IsolationViolation):
                sock.connect(endpoint["value"])
    finally:
        release.set()
        worker.join(5)
    assert outcome == {"paired": True}
