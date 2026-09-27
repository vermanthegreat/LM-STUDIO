"""Phase 3 PostgreSQL E2E acceptance tests for manual paste and /ask safety."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from ask_router import AskIntent, answer_question
from config import AppConfig
from extractor import parse_and_save
from fastapi.testclient import TestClient
from persistence.models import Base, ContactMethod, Organization
from persistence.session import get_engine, reset_cached_engines, session_scope
from repositories.postgres_store import PostgresContactStore
from sqlalchemy import select, text
from tests.pg_support import reset_public_schema, run_alembic_upgrade
from tools.planner import PlannerToolCall
from tools.registry import build_default_registry

from app import create_app

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is not configured",
)

PARSE_PAYLOAD = {
    "company_name": "Phase3 Paste Co",
    "company_email": "paste@phase3.example",
    "confidence": 0.9,
    "people": [{"name": "Pat Lee", "email": "pat@phase3.example", "title": "CEO"}],
}


@pytest.fixture()
def pg_app():
    reset_public_schema(TEST_DATABASE_URL)
    run_alembic_upgrade(TEST_DATABASE_URL, "head")
    reset_cached_engines()
    cfg = AppConfig(database_url=TEST_DATABASE_URL, max_paste_chars=100_000, port=8025)
    store = PostgresContactStore(TEST_DATABASE_URL)
    store.init_db()
    with TestClient(create_app(cfg), base_url="http://127.0.0.1:8025") as client:
        yield client, store, cfg
    engine = get_engine(TEST_DATABASE_URL)
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(text(f'TRUNCATE TABLE "{table.name}" RESTART IDENTITY CASCADE'))
    reset_cached_engines()


def _mark_company_email_verified(store: PostgresContactStore, lead_id: int) -> None:
    with session_scope(store.database_url) as session:
        org = session.scalar(select(Organization).where(Organization.legacy_lead_id == lead_id))
        assert org is not None
        method = session.scalar(
            select(ContactMethod).where(
                ContactMethod.organization_id == org.id,
                ContactMethod.kind == "email",
            )
        )
        assert method is not None
        method.verification_status = "verified"


def test_manual_paste_stores_raw_source_without_auto_committing_people(pg_app):
    client, store, _ = pg_app
    raw_text = "Phase3 Paste Co\npaste@phase3.example\nPat Lee <pat@phase3.example>"

    with patch("extractor.extract_structured", return_value=(PARSE_PAYLOAD, "llm-raw")):
        response = client.post(
            "/parse",
            data={"raw_text": raw_text, "source_type": "website"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    leads = store.list_leads()
    assert any(lead["company_name"] == "Phase3 Paste Co" for lead in leads)
    lead = next(lead for lead in leads if lead["company_name"] == "Phase3 Paste Co")
    detail = store.get_lead(lead["id"])
    assert detail is not None
    assert len(detail.get("raw_sources") or []) == 1
    assert detail["raw_sources"][0]["raw_text"] == raw_text
    assert store.get_extraction_status_for_source(detail["raw_sources"][0]["id"]) == "proposed"
    assert len(detail.get("people") or []) == 0


def test_extracted_contact_proposals_are_unverified_not_verified_truth(pg_app):
    _, store, _ = pg_app
    raw_text = "Phase3 Paste Co\npaste@phase3.example"

    with patch("extractor.extract_structured", return_value=(PARSE_PAYLOAD, "llm-raw")):
        result = parse_and_save("website", raw_text, store=store)

    assert result["people_count"] == 0
    assert store.get_extraction_status_for_source(result["raw_source_id"]) == "proposed"

    registry = build_default_registry()
    unverified = registry.execute(
        store,
        "list_unverified_contact_methods",
        {"kind": "email", "verification_status": "unverified"},
    )
    values = {row["value"] for row in unverified.records}
    assert "paste@phase3.example" in values
    assert "pat@phase3.example" in values
    assert all(row["verification_status"] != "verified" for row in unverified.records)


def test_verified_contact_method_is_not_overwritten_by_conflicting_extraction(pg_app):
    _, store, _ = pg_app
    lead, _ = store.upsert_lead(
        {"company_name": "Verified Co", "company_email": "verified@example.com", "fit_score": 80}
    )
    _mark_company_email_verified(store, lead["id"])

    conflicting = {
        **PARSE_PAYLOAD,
        "company_name": "Verified Co",
        "company_email": "conflicting@example.com",
    }
    with patch("extractor.extract_structured", return_value=(conflicting, "llm-raw")):
        parse_and_save(
            "website",
            "Verified Co\nconflicting@example.com",
            store=store,
            attach_to_lead_id=lead["id"],
        )

    detail = store.get_lead(lead["id"])
    assert detail is not None
    assert detail["company_email"] == "verified@example.com"


def test_ask_search_finds_committed_organization_after_parse(pg_app):
    _, store, _ = pg_app
    with patch("extractor.extract_structured", return_value=(PARSE_PAYLOAD, "llm-raw")):
        parse_and_save("website", "Phase3 Paste Co", store=store)

    result = answer_question(
        "Phase3 Paste Co",
        use_llm=False,
        store=store,
    )
    assert result["intent"] == "search_leads"
    names = {row["company_name"] for row in result["data"]["leads"]}
    assert "Phase3 Paste Co" in names


def test_ask_unverified_list_includes_proposed_and_excludes_verified(pg_app):
    _, store, _ = pg_app
    lead, _ = store.upsert_lead(
        {"company_name": "Verified Co", "company_email": "verified@example.com", "fit_score": 80}
    )
    _mark_company_email_verified(store, lead["id"])

    with patch("extractor.extract_structured", return_value=(PARSE_PAYLOAD, "llm-raw")):
        parse_and_save("website", "Phase3 Paste Co\npaste@phase3.example", store=store)

    registry = build_default_registry()
    result = registry.execute(store, "list_unverified_contact_methods", {"kind": "email"})
    values = {row["value"] for row in result.records}
    statuses = {row["verification_status"] for row in result.records}
    assert "paste@phase3.example" in values
    assert "verified@example.com" not in values
    assert "verified" not in statuses


def test_ask_use_llm_planner_routes_read_tool_without_live_lm_studio(pg_app):
    _, store, _ = pg_app
    planner_payload = PlannerToolCall(
        tool_name="search_contacts",
        arguments={"text": "Phase3", "limit": 10},
        reason="Find pasted organization.",
    )
    with patch("services.llm_planner.plan_question_with_local_llm", return_value=planner_payload):
        with patch("extractor.extract_structured", return_value=(PARSE_PAYLOAD, "llm-raw")):
            parse_and_save("website", "Phase3 Paste Co", store=store)

        with patch("ask_router._deterministic_intent", return_value=AskIntent("unknown")):
            result = answer_question("find phase3 contacts", use_llm=True, store=store)

    assert result["intent"] == "search_contacts"
    assert result["data"]["tool_name"] == "search_contacts"


def test_ask_use_llm_survives_unavailable_local_planner(pg_app):
    _, store, _ = pg_app
    with patch("services.llm_planner.plan_question_with_local_llm", return_value=None):
        with patch("ask_router.call_lmstudio_for_text", return_value=None):
            result = answer_question("what is the meaning of life", use_llm=True, store=store)

    assert result["intent"] == "search_leads"
    assert result["answer"]
