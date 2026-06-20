"""Focused tests for search, followup, and unverified-list tool contract filters."""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import db
import pytest
from ask_router import answer_question
from repositories.sqlite_store import SqliteContactStore
from tools.read_handlers import handle_list_unverified_contact_methods
from tools.read_inputs import ListUnverifiedContactMethodsInput
from tools.registry import ToolValidationError, build_default_registry


def _seed_search(db_path):
    db.init_db(db_path)
    db.upsert_lead(
        {
            "company_name": "Alpha Search Co",
            "company_email": "alpha@example.com",
            "fit_score": 90,
            "status": "new",
        },
        db_path=db_path,
    )
    db.upsert_lead(
        {
            "company_name": "Beta Other",
            "company_email": "beta@example.com",
            "fit_score": 40,
            "status": "new",
        },
        db_path=db_path,
    )


def _seed_followups(db_path):
    db.init_db(db_path)
    lead, _ = db.upsert_lead(
        {"company_name": "Followup Co", "fit_score": 70},
        db_path=db_path,
    )
    today = date.today()
    yesterday = today - timedelta(days=1)
    db.add_task(
        lead["id"],
        {
            "title": "High priority yesterday",
            "due_date": yesterday.isoformat(),
            "priority": "high",
            "status": "open",
        },
        db_path=db_path,
    )
    db.add_task(
        lead["id"],
        {
            "title": "Low priority today",
            "due_date": today.isoformat(),
            "priority": "low",
            "status": "open",
        },
        db_path=db_path,
    )


def _seed_unverified(db_path):
    db.init_db(db_path)
    db.upsert_lead(
        {
            "company_name": "Unverified Email Co",
            "company_email": "unverified@example.com",
            "fit_score": 80,
            "status": "new",
        },
        db_path=db_path,
    )
    db.upsert_lead(
        {
            "company_name": "Low Score Co",
            "company_email": "low@example.com",
            "fit_score": 20,
            "status": "new",
        },
        db_path=db_path,
    )


def test_search_contacts_honors_text_and_limit_filters(tmp_path):
    db_path = tmp_path / "search.db"
    _seed_search(db_path)
    registry = build_default_registry()
    store = SqliteContactStore(db_path)

    result = registry.execute(
        store,
        "search_contacts",
        {"text": "Alpha", "minimum_relevance": 80, "limit": 1},
    )

    assert result.record_count == 1
    assert len(result.records) == 1
    assert result.records[0]["company_name"] == "Alpha Search Co"


def test_search_contacts_rejects_unsupported_planner_filters(tmp_path):
    registry = build_default_registry()
    with pytest.raises(ToolValidationError):
        registry.validate_arguments(
            "search_contacts",
            {"text": "Alpha", "query": "Alpha", "sql": "SELECT 1"},
        )


def test_followups_honor_priority_and_due_date_filters(tmp_path):
    db_path = tmp_path / "followups.db"
    _seed_followups(db_path)
    registry = build_default_registry()
    store = SqliteContactStore(db_path)
    yesterday = date.today() - timedelta(days=1)

    result = registry.execute(
        store,
        "list_due_followups",
        {
            "priority": "high",
            "due_on_or_before": yesterday.isoformat(),
            "item_type": "task",
        },
    )

    assert result.record_count == 1
    assert result.records[0]["title"] == "High priority yesterday"
    assert "priority=high" in result.warnings


def test_followups_reject_unsupported_planner_filters(tmp_path):
    registry = build_default_registry()
    with pytest.raises(ToolValidationError):
        registry.validate_arguments(
            "list_due_followups",
            {"priority": "high", "lead_id": 1, "sql": "bad"},
        )


def test_unverified_list_honors_kind_and_relevance_filters(tmp_path):
    db_path = tmp_path / "unverified.db"
    _seed_unverified(db_path)
    registry = build_default_registry()
    store = SqliteContactStore(db_path)

    result = registry.execute(
        store,
        "list_unverified_contact_methods",
        {
            "kind": "email",
            "minimum_relevance": 50,
            "verification_status": "unverified",
        },
    )

    assert result.record_count == 1
    assert result.records[0]["company_name"] == "Unverified Email Co"
    assert result.records[0]["verification_status"] == "unverified"
    assert "verification_scope=non_verified_only" in result.warnings


def test_unverified_list_rejects_verified_status_filter(tmp_path):
    registry = build_default_registry()
    with pytest.raises(ToolValidationError):
        registry.validate_arguments(
            "list_unverified_contact_methods",
            {"verification_status": "verified"},
        )


def test_unverified_list_never_returns_verified_records():
    store = SimpleNamespace(
        backend="postgresql",
        list_contact_method_records=lambda: [
            {
                "lead_id": 1,
                "company_name": "Verified Co",
                "person_name": None,
                "kind": "email",
                "value": "verified@example.com",
                "verification_status": "verified",
                "organization_status": "new",
                "fit_score": 80,
            },
            {
                "lead_id": 2,
                "company_name": "Pending Co",
                "person_name": None,
                "kind": "email",
                "value": "pending@example.com",
                "verification_status": "unverified",
                "organization_status": "new",
                "fit_score": 80,
            },
        ],
    )
    result = handle_list_unverified_contact_methods(
        store,
        ListUnverifiedContactMethodsInput(),
    )
    assert result.record_count == 1
    assert result.records[0]["value"] == "pending@example.com"
    assert all(row["verification_status"] != "verified" for row in result.records)


def test_deterministic_followups_path_unchanged_without_llm(tmp_path):
    db_path = tmp_path / "ask.db"
    _seed_followups(db_path)
    store = SqliteContactStore(db_path)

    with patch("services.llm_planner.plan_question_with_local_llm") as planner:
        result = answer_question("show follow-ups due", use_llm=False, store=store)

    planner.assert_not_called()
    assert result["intent"] == "followups_due"
    assert result["data"]["tool_name"] == "list_due_followups"
    assert result["data"]["record_count"] == 2
