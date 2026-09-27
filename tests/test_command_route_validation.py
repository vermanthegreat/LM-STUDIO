"""HTTP validation for /ask command routes."""

from __future__ import annotations

from uuid import uuid4

import db
import pytest
from fastapi.testclient import TestClient

from app import create_app
from config import AppConfig


def _client(tmp_path):
    db_path = tmp_path / "routes.db"
    cfg = AppConfig(database_path=db_path, max_paste_chars=1000, port=8025)
    client = TestClient(create_app(cfg), base_url="http://127.0.0.1:8025")
    client.__enter__()
    return client


def _close_client(client):
    client.__exit__(None, None, None)


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/ask/commands/not-a-uuid"),
        ("post", "/ask/commands/not-a-uuid/approve"),
        ("post", "/ask/commands/not-a-uuid/apply"),
    ],
)
def test_invalid_command_id_returns_422(tmp_path, method, path):
    client = _client(tmp_path)
    try:
        response = client.request(method, path)
        assert response.status_code == 422
        body = response.json()
        assert body["error_code"] == "invalid_command_id"
        assert "Invalid command_id" in body["message"]
    finally:
        _close_client(client)


def test_unknown_valid_command_id_returns_404(tmp_path):
    client = _client(tmp_path)
    try:
        missing_id = uuid4()
        response = client.get(f"/ask/commands/{missing_id}")
        assert response.status_code == 404
        body = response.json()
        assert body["status"] == "error"
        assert body["data"]["error_code"] == "command_not_found"
    finally:
        _close_client(client)


def test_invalid_command_id_apply_does_not_return_500(tmp_path):
    client = _client(tmp_path)
    try:
        response = client.post("/ask/commands/bad-id/apply")
        assert response.status_code == 422
        assert response.status_code != 500
    finally:
        _close_client(client)
