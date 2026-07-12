"""Gmail G0 read-only integration tests (fake provider only)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import db
import pytest
from config import AppConfig
from fastapi.testclient import TestClient
from gmail_schemas import GMAIL_READONLY_SCOPE, AttentionMarker, PrimaryIntent
from providers.fake_gmail import FakeGmailProvider
from providers.gmail_normalize import normalize_gmail_api_message
from repositories.sqlite_store import SqliteContactStore
from services.command_log import CommandStatus
from services.email_classification_service import classify_email_message, determine_direction
from services.gmail_linking import resolve_contact_link
from services.gmail_sync_service import gmail_integration_status, sync_gmail_label
from tools.registry import build_default_registry

from app import create_app


@pytest.fixture
def sqlite_store(tmp_path):
    store = SqliteContactStore(tmp_path / "gmail.db")
    store.init_db()
    return store


@pytest.fixture
def gmail_cfg(tmp_path):
    secret = tmp_path / "client_secret.json"
    secret.write_text('{"installed":{"client_id":"x","client_secret":"y"}}', encoding="utf-8")
    token = tmp_path / "token.json"
    return AppConfig(
        database_path=tmp_path / "gmail.db",
        gmail_enabled=True,
        gmail_client_secret_path=secret,
        gmail_token_path=token,
        gmail_sync_label="LMStudio",
        gmail_sync_limit=100,
        app_timezone="Asia/Jerusalem",
    )


def _seed_provider() -> FakeGmailProvider:
    provider = FakeGmailProvider("operator@example.com")
    provider.seed_message(
        message_id="msg-1",
        thread_id="thread-abc",
        subject="Interested in partnership",
        from_email="client@agency.com",
        to_email="operator@example.com",
        body="We are interested in moving forward. Can we schedule a call next week?",
    )
    return provider


def test_gmail_disabled_by_default():
    cfg = AppConfig.from_env()
    assert cfg.gmail_enabled is False


def test_missing_token_controlled_error(sqlite_store, gmail_cfg):
    result, entry = sync_gmail_label(sqlite_store, gmail_cfg)
    assert result.status == "error"
    assert result.error_code in {"gmail_token_missing", "gmail_not_configured", "gmail_disabled"}
    if entry:
        assert entry.intent == "gmail_sync"


def test_scope_is_readonly_only():
    assert GMAIL_READONLY_SCOPE == "https://www.googleapis.com/auth/gmail.readonly"


def test_plain_text_normalization():
    provider = _seed_provider()
    message = provider.get_message("msg-1")
    assert "interested" in (message.plain_body or "").lower()
    assert message.thread_id == "thread-abc"
    assert message.internal_date.tzinfo is not None


def test_html_fallback_normalization():
    provider = FakeGmailProvider()
    provider.seed_html_message(
        message_id="html-1",
        thread_id="thread-html",
        subject="HTML only",
        from_email="sender@agency.com",
        to_email="operator@example.com",
        html_body="<html><body><p>Hello <b>team</b></p></body></html>",
    )
    message = provider.get_message("html-1")
    assert "Hello" in (message.plain_body or "")
    assert "team" in (message.plain_body or "")


def test_inbound_outbound_direction():
    provider = _seed_provider()
    inbound = provider.get_message("msg-1")
    assert determine_direction(inbound).value == "inbound"
    provider.seed_message(
        message_id="msg-out",
        thread_id="thread-out",
        subject="Follow up",
        from_email="operator@example.com",
        to_email="client@agency.com",
        body="Following up on our conversation.",
    )
    outbound = provider.get_message("msg-out")
    assert determine_direction(outbound).value == "outbound"


def test_first_sync_imports_message(sqlite_store, gmail_cfg):
    provider = _seed_provider()
    result, entry = sync_gmail_label(sqlite_store, gmail_cfg, provider=provider, use_llm=False)
    assert result.status == "ok"
    assert result.counts.imported == 1
    records, total = sqlite_store.list_imported_email_messages()
    assert total == 1
    assert records[0]["primary_intent"] == "positive_interest"
    assert entry is not None
    assert entry.status == CommandStatus.SUCCEEDED
    assert entry.risk_class == "external"


def test_repeated_sync_idempotent(sqlite_store, gmail_cfg):
    provider = _seed_provider()
    sync_gmail_label(sqlite_store, gmail_cfg, provider=provider, use_llm=False)
    result, _ = sync_gmail_label(sqlite_store, gmail_cfg, provider=provider, use_llm=False)
    assert result.counts.already_present == 1
    assert result.counts.imported == 0
    _, total = sqlite_store.list_imported_email_messages()
    assert total == 1


def test_exact_person_email_links(sqlite_store, gmail_cfg):
    lead, _ = sqlite_store.upsert_lead({"company_name": "Agency Co", "company_email": "info@agency.com"})
    sqlite_store.add_person(lead["id"], {"name": "Pat", "email": "pat@agency.com"})
    provider = FakeGmailProvider()
    provider.seed_message(
        message_id="m-link",
        thread_id="t-link",
        subject="Question",
        from_email="Pat <pat@agency.com>",
        to_email="operator@example.com",
        body="Can you share pricing?",
    )
    sync_gmail_label(sqlite_store, gmail_cfg, provider=provider, use_llm=False)
    records, _ = sqlite_store.list_imported_email_messages()
    assert records[0]["link_status"] == "linked"
    assert records[0]["lead_id"] == lead["id"]


def test_domain_only_stays_unlinked(sqlite_store, gmail_cfg):
    sqlite_store.upsert_lead({"company_name": "Other Co", "website": "https://other.com", "domain": "other.com"})
    provider = FakeGmailProvider()
    provider.seed_message(
        message_id="m-domain",
        thread_id="t-domain",
        subject="Hello",
        from_email="someone@agency.com",
        to_email="operator@example.com",
        body="Hello there",
    )
    sync_gmail_label(sqlite_store, gmail_cfg, provider=provider, use_llm=False)
    records, _ = sqlite_store.list_imported_email_messages()
    assert records[0]["link_status"] == "unlinked"
    assert db.count_potential_clients(db_path=sqlite_store.database_path) == 1


def test_no_automatic_task_created(sqlite_store, gmail_cfg):
    provider = _seed_provider()
    sync_gmail_label(sqlite_store, gmail_cfg, provider=provider, use_llm=False)
    with db.get_conn(sqlite_store.database_path) as conn:
        tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    assert tasks == 0


def test_classification_automated_header():
    provider = FakeGmailProvider()
    provider.seed_message(
        message_id="auto-1",
        thread_id="t-auto",
        subject="Notification",
        from_email="no-reply@service.com",
        to_email="operator@example.com",
        body="Your report is ready.",
        headers={"Auto-Submitted": "auto-generated"},
    )
    message = provider.get_message("auto-1")
    result = classify_email_message(message, link_status="unlinked", use_llm=False)
    assert result.primary_intent == PrimaryIntent.AUTOMATED
    assert AttentionMarker.AUTOMATED_MESSAGE in result.markers


def test_malformed_llm_fallback():
    provider = _seed_provider()
    message = provider.get_message("msg-1")
    with patch("services.email_classification_service.chat_completion", return_value="not json"):
        result = classify_email_message(message, link_status="unlinked", use_llm=True)
    assert result.primary_intent in {PrimaryIntent.POSITIVE_INTEREST, PrimaryIntent.UNKNOWN}


def test_unknown_markers_rejected():
    from gmail_schemas import validate_markers

    markers = validate_markers(["reply_needed", "totally_fake_marker"])
    assert markers == [AttentionMarker.REPLY_NEEDED]


def test_gmail_status_and_sync_routes(tmp_path, gmail_cfg):
    client = TestClient(create_app(gmail_cfg), base_url="http://127.0.0.1:8025")
    with client:
        response = client.get("/integrations/gmail")
        assert response.status_code == 200
        assert "Gmail Integration" in response.text

        bad_origin = client.post(
            "/integrations/gmail/sync",
            headers={"Origin": "http://evil.example"},
        )
        assert bad_origin.status_code == 403

        provider = _seed_provider()
        with patch("services.gmail_sync_service.sync_gmail_label") as mocked:
            mocked.return_value = (
                type("R", (), {
                    "status": "ok",
                    "counts": type("C", (), {
                        "imported": 1,
                        "updated": 0,
                        "already_present": 0,
                        "failed": 0,
                    })(),
                    "message": None,
                    "error_code": None,
                })(),
                None,
            )
            response = client.post("/integrations/gmail/sync")
        assert response.status_code == 200


def test_list_email_messages_tool(sqlite_store, gmail_cfg):
    provider = _seed_provider()
    sync_gmail_label(sqlite_store, gmail_cfg, provider=provider, use_llm=False)
    registry = build_default_registry()
    result = registry.execute(sqlite_store, "list_email_messages", {"marker": "positive_signal", "limit": 10})
    assert result.status == "ok"
    assert result.record_count >= 1


def test_get_email_thread_tool(sqlite_store, gmail_cfg):
    provider = _seed_provider()
    sync_gmail_label(sqlite_store, gmail_cfg, provider=provider, use_llm=False)
    registry = build_default_registry()
    result = registry.execute(
        sqlite_store,
        "get_email_thread",
        {"external_thread_id": "thread-abc"},
    )
    assert result.record_count == 1


def test_command_log_has_no_secrets(sqlite_store, gmail_cfg, caplog):
    caplog.set_level(logging.INFO)
    token = gmail_cfg.gmail_token_path
    token.write_text('{"token":"secret-value","refresh_token":"refresh-secret"}', encoding="utf-8")
    provider = _seed_provider()
    _, entry = sync_gmail_label(sqlite_store, gmail_cfg, provider=provider, use_llm=False)
    blob = json.dumps(entry.result_summary or {}) + (entry.error_message or "")
    assert "refresh-secret" not in blob
    assert "secret-value" not in blob


def test_label_missing_error(sqlite_store, gmail_cfg):
    provider = FakeGmailProvider()
    provider._labels = [{"id": "INBOX", "name": "INBOX"}]
    result, entry = sync_gmail_label(sqlite_store, gmail_cfg, provider=provider, use_llm=False)
    assert result.error_code == "gmail_label_missing"
    assert entry.status == CommandStatus.FAILED


@pytest.fixture
def pg_store():
    from repositories.postgres_store import PostgresContactStore

    return PostgresContactStore("postgresql://example.invalid/contacts")


def test_postgres_gmail_list_raises(pg_store):
    from gmail_runtime import GmailRuntimeUnsupportedError

    with pytest.raises(GmailRuntimeUnsupportedError) as exc_info:
        pg_store.list_imported_email_messages()
    assert exc_info.value.error_code == "gmail_postgresql_runtime_unsupported"


def test_postgres_gmail_thread_raises(pg_store):
    from gmail_runtime import GmailRuntimeUnsupportedError

    with pytest.raises(GmailRuntimeUnsupportedError):
        pg_store.get_imported_email_thread("thread-abc")


def test_postgres_gmail_sync_fails_closed(pg_store, gmail_cfg):
    result, entry = sync_gmail_label(pg_store, gmail_cfg, provider=_seed_provider(), use_llm=False)
    assert result.status == "error"
    assert result.error_code == "gmail_postgresql_runtime_unsupported"
    assert entry is not None
    assert entry.status == CommandStatus.FAILED
    assert entry.error_code == "gmail_postgresql_runtime_unsupported"


def test_postgres_gmail_status_reports_unsupported(pg_store, gmail_cfg):
    status = gmail_integration_status(pg_store, gmail_cfg)
    assert status["runtime_capability"] == "postgresql_unsupported"
    assert status["runtime_backend"] == "postgresql"


def test_postgres_list_email_messages_tool_fails_closed(pg_store):
    registry = build_default_registry()
    result = registry.execute(pg_store, "list_email_messages", {"limit": 10})
    assert result.status == "error"
    assert result.record_count == 0
    assert "gmail_postgresql_runtime_unsupported" in result.warnings


def test_postgres_get_email_thread_tool_fails_closed(pg_store):
    registry = build_default_registry()
    result = registry.execute(
        pg_store,
        "get_email_thread",
        {"external_thread_id": "thread-abc"},
    )
    assert result.status == "error"
    assert result.record_count == 0
    assert "gmail_postgresql_runtime_unsupported" in result.warnings


def test_no_automatic_lead_or_person_created(sqlite_store, gmail_cfg):
    provider = _seed_provider()
    before_leads = db.count_potential_clients(db_path=sqlite_store.database_path)
    with db.get_conn(sqlite_store.database_path) as conn:
        before_people = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    sync_gmail_label(sqlite_store, gmail_cfg, provider=provider, use_llm=False)
    after_leads = db.count_potential_clients(db_path=sqlite_store.database_path)
    with db.get_conn(sqlite_store.database_path) as conn:
        after_people = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    assert after_leads == before_leads
    assert after_people == before_people


def test_provider_message_failure_does_not_advance_success(sqlite_store, gmail_cfg):
    provider = _seed_provider()
    provider.seed_message(
        message_id="msg-bad",
        thread_id="thread-abc",
        subject="Broken",
        from_email="broken@agency.com",
        to_email="operator@example.com",
        body="This one will fail.",
    )

    def _boom(message_id: str):
        if message_id == "msg-bad":
            raise RuntimeError("provider read failed")
        return provider._messages[message_id]

    provider.get_message = _boom  # type: ignore[method-assign]
    result, _ = sync_gmail_label(sqlite_store, gmail_cfg, provider=provider, use_llm=False)
    assert result.status == "ok"
    assert result.counts.failed >= 1
    import gmail_db

    with db.get_conn(sqlite_store.database_path) as conn:
        gmail_db.ensure_gmail_tables(conn)
        state = gmail_db.get_sync_state(conn, configured_label=gmail_cfg.gmail_sync_label)
    assert state.get("last_status") in {"partial", "error"}
    assert state.get("last_success_at") is None


def test_sqlite_gmail_status_reports_sqlite_only(sqlite_store, gmail_cfg):
    status = gmail_integration_status(sqlite_store, gmail_cfg)
    assert status["runtime_capability"] == "sqlite_only"
    assert status["runtime_backend"] == "sqlite"
