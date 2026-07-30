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
from gmail_schemas import GMAIL_READONLY_SCOPE, AttentionMarker, MessageRole, PrimaryIntent
from providers.fake_gmail import FakeGmailProvider
from providers.gmail_normalize import normalize_gmail_api_message
from repositories.sqlite_store import SqliteContactStore
from services.command_log import CommandStatus
from services.email_classification_service import classify_email_message, determine_direction
from services.gmail_linking import resolve_contact_link
from services.gmail_sync_service import gmail_integration_status, sync_gmail_label
from tools.registry import build_default_registry
from ask_router import answer_question

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


def _records_by_message_id(sqlite_store) -> dict[str, dict]:
    records, _ = sqlite_store.list_imported_email_messages(limit=100)
    return {str(row["external_message_id"]): row for row in records}


def _seed_shopify_confirmation(
    provider: FakeGmailProvider,
    *,
    message_id: str = "shopify-relay-1",
    thread_id: str = "t-shopify-relay",
    target_company: str = "Human Element",
    initiator: str = "Operator",
) -> None:
    provider.seed_message(
        message_id=message_id,
        thread_id=thread_id,
        subject=f"Shopify Partner Directory: New Service Inquiry from {initiator} to {target_company}",
        from_email="Shopify Partner Directory <partners@shopify.com>",
        to_email="operator@example.com, archive@example.com",
        body=(
            "Shopify Partner Directory\n\n"
            "Your message has been sent. "
            "You asked whether they can schedule a call next week."
        ),
    )


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


def test_shopify_partner_directory_confirmation_has_typed_role_outreach_and_target(sqlite_store, gmail_cfg):
    lead, _ = sqlite_store.upsert_lead({"company_name": "Human Element"})
    provider = FakeGmailProvider()
    _seed_shopify_confirmation(provider, target_company="Human Element to Consumer")
    sqlite_store.upsert_lead({"company_name": "Human Element to Consumer"})
    sync_gmail_label(sqlite_store, gmail_cfg, provider=provider, use_llm=False)

    records, total = sqlite_store.list_imported_email_messages()
    assert total == 1
    row = records[0]
    assert row["direction"] == "inbound"
    assert row["message_role"] == MessageRole.SHOPIFY_PARTNER_INQUIRY_CONFIRMATION.value
    assert row["target_company_name"] == "Human Element to Consumer"
    assert row["primary_intent"] == "outreach"
    assert row["requires_followup"] is False
    assert row["link_status"] == "linked"
    assert row["lead_id"] != lead["id"]
    assert row["classification_warning"] == "shopify_partner_directory_relay"
    assert "automated_message" in row["markers"]
    assert "reply_needed" not in row["markers"]
    assert "meeting_requested" not in row["markers"]
    assert row["external_account"] == "operator@example.com"
    assert row["to_address_text"] == "operator@example.com, archive@example.com"


def test_shopify_confirmation_ambiguous_on_multiple_exact_company_matches(sqlite_store, gmail_cfg):
    with db.get_conn(sqlite_store.database_path) as conn:
        now = datetime.now(timezone.utc).isoformat()
        normalized = db.normalize_name("Human Element")
        conn.execute(
            "INSERT INTO leads (company_name, normalized_name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("Human Element", normalized, now, now),
        )
        conn.execute(
            "INSERT INTO leads (company_name, normalized_name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("Human Element", normalized, now, now),
        )
    provider = FakeGmailProvider()
    _seed_shopify_confirmation(provider, target_company="Human Element")
    sync_gmail_label(sqlite_store, gmail_cfg, provider=provider, use_llm=False)
    row = _records_by_message_id(sqlite_store)["shopify-relay-1"]
    assert row["link_status"] == "ambiguous"
    assert row["lead_id"] is None


def test_shopify_confirmation_never_links_shopify_sender_as_agency(sqlite_store, gmail_cfg):
    sqlite_store.upsert_lead({"company_name": "Shopify Transport", "company_email": "partners@shopify.com"})
    provider = FakeGmailProvider()
    _seed_shopify_confirmation(provider, target_company="Missing Agency")
    sync_gmail_label(sqlite_store, gmail_cfg, provider=provider, use_llm=False)
    row = _records_by_message_id(sqlite_store)["shopify-relay-1"]
    assert row["link_status"] == "unlinked"
    assert row["lead_id"] is None


def test_direct_agency_reply_inherits_thread_link_without_confirmation_role(sqlite_store, gmail_cfg):
    lead, _ = sqlite_store.upsert_lead({"company_name": "Human Element"})
    provider = FakeGmailProvider()
    _seed_shopify_confirmation(provider, message_id="shopify-confirm", thread_id="thread-human", target_company="Human Element")
    provider.seed_message(
        message_id="agency-reply",
        thread_id="thread-human",
        subject="Re: Shopify Partner Directory: New Service Inquiry from Operator to Human Element",
        from_email="hello@humanelement.com",
        to_email="operator@example.com",
        body="Sounds good, we are interested in talking further.",
        internal_date=datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc),
    )
    sync_gmail_label(sqlite_store, gmail_cfg, provider=provider, use_llm=False)
    rows = _records_by_message_id(sqlite_store)
    assert rows["shopify-confirm"]["lead_id"] == lead["id"]
    assert rows["agency-reply"]["lead_id"] == lead["id"]
    assert rows["agency-reply"]["message_role"] == "conversation_message"
    assert rows["agency-reply"]["primary_intent"] == "positive_interest"


def test_existing_misclassified_shopify_confirmation_is_reclassified_idempotently(sqlite_store, gmail_cfg):
    sqlite_store.upsert_lead({"company_name": "Human Element"})
    provider = FakeGmailProvider()
    _seed_shopify_confirmation(provider, target_company="Human Element")
    sync_gmail_label(sqlite_store, gmail_cfg, provider=provider, use_llm=False)
    before = _records_by_message_id(sqlite_store)["shopify-relay-1"]
    with db.get_conn(sqlite_store.database_path) as conn:
        conn.execute(
            """
            UPDATE gmail_messages
            SET message_role = 'conversation_message',
                target_company_name = NULL,
                primary_intent = 'meeting_or_call_request',
                markers_json = ?,
                requires_followup = 1,
                link_status = 'unlinked',
                lead_id = NULL,
                classification_warning = NULL
            WHERE id = ?
            """,
            (json.dumps(["meeting_requested", "reply_needed"]), before["id"]),
        )

    correction, _ = sync_gmail_label(sqlite_store, gmail_cfg, provider=provider, use_llm=False)
    corrected = _records_by_message_id(sqlite_store)["shopify-relay-1"]
    records_after_correction, total_after_correction = sqlite_store.list_imported_email_messages()
    repeated, _ = sync_gmail_label(sqlite_store, gmail_cfg, provider=provider, use_llm=False)
    repeated_row = _records_by_message_id(sqlite_store)["shopify-relay-1"]
    records_after_repeat, total_after_repeat = sqlite_store.list_imported_email_messages()

    assert correction.counts.updated == 1
    assert corrected["external_message_id"] == before["external_message_id"]
    assert corrected["external_thread_id"] == before["external_thread_id"]
    assert total_after_correction == 1
    assert corrected["message_role"] == MessageRole.SHOPIFY_PARTNER_INQUIRY_CONFIRMATION.value
    assert corrected["primary_intent"] == "outreach"
    assert corrected["requires_followup"] is False
    assert "meeting_requested" not in corrected["markers"]
    assert "reply_needed" not in corrected["markers"]
    assert records_after_correction[0]["id"] == before["id"]
    assert repeated.counts.updated == 0
    assert total_after_repeat == 1
    assert repeated_row == corrected


def test_label_boundary_passes_resolved_label_id_and_no_sender_exclusion(sqlite_store, gmail_cfg):
    provider = FakeGmailProvider()
    provider._labels = [{"id": "Label_Custom", "name": "LMStudio", "type": "user"}]
    provider.seed_message(
        message_id="google-alert-labeled",
        thread_id="thread-alert",
        subject="Security alert",
        from_email="Google <no-reply@accounts.google.com>",
        to_email="operator@example.com",
        body="A security alert carried the configured label.",
        label_ids=["Label_Custom"],
    )
    provider.seed_message(
        message_id="google-alert-unlabeled",
        thread_id="thread-alert-2",
        subject="Security alert",
        from_email="Google <no-reply@accounts.google.com>",
        to_email="operator@example.com",
        body="This alert does not carry the configured label.",
        label_ids=["INBOX"],
    )
    result, entry = sync_gmail_label(sqlite_store, gmail_cfg, provider=provider, use_llm=False)
    rows = _records_by_message_id(sqlite_store)
    assert result.counts.discovered == 1
    assert result.counts.imported == 1
    assert provider.list_message_calls == [{"label_id": "Label_Custom", "limit": 100, "page_token": None}]
    assert entry is not None
    assert entry.tool_arguments["label_id"] == "Label_Custom"
    assert "google-alert-labeled" in rows
    assert "google-alert-unlabeled" not in rows


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


def test_gmail_page_displays_recent_imported_email_data(sqlite_store, gmail_cfg):
    provider = _seed_provider()
    sync_gmail_label(sqlite_store, gmail_cfg, provider=provider, use_llm=False)

    client = TestClient(create_app(gmail_cfg), base_url="http://127.0.0.1:8025")
    with client:
        response = client.get("/integrations/gmail")

    assert response.status_code == 200
    assert "Recent Imported Email Data" in response.text
    assert "operator@example.com" in response.text
    assert "client@agency.com" in response.text
    assert "Interested in partnership" in response.text
    assert "Body preview" in response.text
    assert "positive_interest" in response.text


def test_imported_emails_page_displays_email_relevant_fields(sqlite_store, gmail_cfg):
    provider = _seed_provider()
    sync_gmail_label(sqlite_store, gmail_cfg, provider=provider, use_llm=False)

    client = TestClient(create_app(gmail_cfg), base_url="http://127.0.0.1:8025")
    with client:
        response = client.get("/emails")

    assert response.status_code == 200
    assert "Account" in response.text
    assert "From:" in response.text
    assert "To:" in response.text
    assert "Body preview" in response.text
    assert "follow-up" in response.text


def test_imported_emails_page_displays_shopify_confirmation_semantics(sqlite_store, gmail_cfg):
    sqlite_store.upsert_lead({"company_name": "Human Element"})
    provider = FakeGmailProvider()
    _seed_shopify_confirmation(provider, target_company="Human Element")
    sync_gmail_label(sqlite_store, gmail_cfg, provider=provider, use_llm=False)

    client = TestClient(create_app(gmail_cfg), base_url="http://127.0.0.1:8025")
    with client:
        response = client.get("/emails")

    assert response.status_code == 200
    assert "inbound" in response.text
    assert "Shopify inquiry confirmation" in response.text
    assert "Target:" in response.text
    assert "Human Element" in response.text
    assert "outreach" in response.text
    assert "operator@example.com" in response.text
    assert "partners@shopify.com" in response.text
    assert "Link status" in response.text


def test_list_email_messages_tool(sqlite_store, gmail_cfg):
    provider = _seed_provider()
    sync_gmail_label(sqlite_store, gmail_cfg, provider=provider, use_llm=False)
    registry = build_default_registry()
    result = registry.execute(sqlite_store, "list_email_messages", {"marker": "positive_signal", "limit": 10})
    assert result.status == "ok"
    assert result.record_count >= 1


def test_typed_queries_exclude_shopify_confirmation_but_keep_direct_agency_replies(sqlite_store, gmail_cfg):
    sqlite_store.upsert_lead({"company_name": "Pictonix"})
    sqlite_store.upsert_lead({"company_name": "Blackbelt Commerce"})
    provider = FakeGmailProvider()
    _seed_shopify_confirmation(
        provider,
        message_id="confirm-pictonix",
        thread_id="thread-pictonix",
        target_company="Pictonix",
        initiator="Operator",
    )
    provider.seed_message(
        message_id="pictonix-reply",
        thread_id="thread-pictonix",
        subject="Re: Shopify Partner Directory: New Service Inquiry from Operator to Pictonix",
        from_email="team@pictonix.com",
        to_email="operator@example.com",
        body="Sounds good, we are interested.",
        internal_date=datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc),
    )
    provider.seed_message(
        message_id="blackbelt-meeting",
        thread_id="thread-blackbelt",
        subject="Re: Project",
        from_email="hello@blackbeltcommerce.com",
        to_email="operator@example.com",
        body="Can we schedule a call next week?",
        internal_date=datetime(2026, 7, 10, 11, 0, tzinfo=timezone.utc),
    )
    provider.seed_message(
        message_id="operator-outbound",
        thread_id="thread-outbound",
        subject="Following up",
        from_email="operator@example.com",
        to_email="agency@example.com",
        body="Sounds good, looking forward to it.",
        internal_date=datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc),
    )
    sync_gmail_label(sqlite_store, gmail_cfg, provider=provider, use_llm=False)

    positive = answer_question("show positive agency replies", store=sqlite_store, use_llm=False)
    reply_needed = answer_question("show emails that need reply", store=sqlite_store, use_llm=False)
    meetings = answer_question("show meeting requests", store=sqlite_store, use_llm=False)

    positive_ids = {row["external_message_id"] for row in positive["data"]["emails"]}
    reply_ids = {row["external_message_id"] for row in reply_needed["data"]["emails"]}
    meeting_ids = {row["external_message_id"] for row in meetings["data"]["emails"]}
    assert "pictonix-reply" in positive_ids
    assert "operator-outbound" not in positive_ids
    assert "confirm-pictonix" not in positive_ids
    assert "confirm-pictonix" not in reply_ids
    assert "blackbelt-meeting" in meeting_ids
    assert "confirm-pictonix" not in meeting_ids


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
