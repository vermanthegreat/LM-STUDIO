"""HTTP route tests for Phase 0 hardening."""

from __future__ import annotations

from unittest.mock import patch

import db
from config import AppConfig
from fastapi.testclient import TestClient
from repositories.sqlite_store import SqliteContactStore

from app import create_app


def _client(tmp_path):
    db_path = tmp_path / "routes.db"
    cfg = AppConfig(database_path=db_path, max_paste_chars=1000, port=8025)
    client = TestClient(create_app(cfg), base_url="http://127.0.0.1:8025")
    client.__enter__()
    return client, db_path, cfg


def _close_client(client):
    client.__exit__(None, None, None)


def test_parse_success_redirects(tmp_path):
    client, db_path, _ = _client(tmp_path)
    try:
        parsed = {
            "company_name": "Route Test Co",
            "confidence": 0.9,
            "people": [],
        }
        with patch("extractor.extract_structured", return_value=(parsed, None)):
            response = client.post(
                "/parse",
                data={
                    "raw_text": "Route Test Co\nhttps://route-test.example",
                    "source_type": "website",
                },
                follow_redirects=False,
            )
        assert response.status_code == 303
        assert "msg=Saved" in response.headers["location"]
        assert db.count_potential_clients(db_path=db_path) == 1
    finally:
        _close_client(client)


def test_parse_rejects_empty_text(tmp_path):
    client, _, _ = _client(tmp_path)
    try:
        response = client.post(
            "/parse",
            data={"raw_text": "   ", "source_type": "website"},
        )
        assert response.status_code == 422
        assert response.json()["error_code"] == "empty_raw_text"
    finally:
        _close_client(client)


def test_parse_rejects_invalid_source_type(tmp_path):
    client, _, _ = _client(tmp_path)
    try:
        response = client.post(
            "/parse",
            data={"raw_text": "hello", "source_type": "invalid_type"},
        )
        assert response.status_code == 422
        assert response.json()["error_code"] == "invalid_source_type"
    finally:
        _close_client(client)


def test_parse_rejects_oversized_text(tmp_path):
    client, _, _ = _client(tmp_path)
    try:
        response = client.post(
            "/parse",
            data={"raw_text": "x" * 1001, "source_type": "note"},
        )
        assert response.status_code == 422
        assert response.json()["error_code"] == "raw_text_too_long"
    finally:
        _close_client(client)


def test_parse_rejects_invalid_url(tmp_path):
    client, _, _ = _client(tmp_path)
    try:
        response = client.post(
            "/parse",
            data={"raw_text": "hello", "source_type": "note", "source_url": "ftp://bad.example"},
        )
        assert response.status_code == 422
        assert response.json()["error_code"] == "invalid_source_url"
    finally:
        _close_client(client)


def test_parse_rejects_missing_attach_lead(tmp_path):
    client, _, _ = _client(tmp_path)
    try:
        response = client.post(
            "/parse",
            data={"raw_text": "hello", "source_type": "note", "attach_to_lead_id": "999"},
        )
        assert response.status_code == 404
    finally:
        _close_client(client)


def test_lead_detail_missing_returns_404(tmp_path):
    client, _, _ = _client(tmp_path)
    try:
        response = client.get("/leads/404")
        assert response.status_code == 404
    finally:
        _close_client(client)


def test_unsafe_origin_blocked_on_parse(tmp_path):
    client, _, _ = _client(tmp_path)
    try:
        response = client.post(
            "/parse",
            data={"raw_text": "hello", "source_type": "note"},
            headers={"Origin": "https://evil.example"},
        )
        assert response.status_code == 403
        assert response.json()["error_code"] == "unsafe_origin"
    finally:
        _close_client(client)


def test_csv_formula_hardening(tmp_path):
    client, db_path, _ = _client(tmp_path)
    try:
        db.upsert_lead({"company_name": "=HYPERLINK()", "company_email": "+123"}, db_path=db_path)

        response = client.get("/export/csv")
        assert response.status_code == 200
        body = response.text
        assert "'=HYPERLINK()" in body
        assert "'+123" in body
    finally:
        _close_client(client)


def test_parse_rolls_back_on_persistence_failure(tmp_path):
    client, db_path, _ = _client(tmp_path)
    try:
        parsed = {"company_name": "Rollback Co", "confidence": 0.9, "people": []}

        with patch("extractor.extract_structured", return_value=(parsed, None)):
            with patch.object(SqliteContactStore, "create_raw_source", side_effect=RuntimeError("boom")):
                response = client.post(
                    "/parse",
                    data={"raw_text": "Rollback Co", "source_type": "note"},
                )
        assert response.status_code == 500
        assert db.count_potential_clients(db_path=db_path) == 0
    finally:
        _close_client(client)
