"""Tests for Phase 2 typed tool registry."""

from __future__ import annotations

import db
import pytest
from pydantic import ValidationError
from repositories.sqlite_store import SqliteContactStore
from tools.planner import PlannerToolCall
from tools.registry import (
    ToolRegistry,
    ToolValidationError,
    UnknownToolError,
    build_default_registry,
)
from services.command_service import CommandService


def _seed(db_path):
    db.init_db(db_path)
    db.upsert_lead(
        {
            "company_name": "With Email Co",
            "company_email": "hello@example.com",
            "fit_score": 80,
        },
        db_path=db_path,
    )
    db.upsert_lead({"company_name": "No Email Co", "fit_score": 40}, db_path=db_path)


def test_unknown_tool_rejected():
    registry = build_default_registry()
    with pytest.raises(UnknownToolError):
        registry.get("run_sql")


def test_unknown_fields_rejected():
    registry = build_default_registry()
    with pytest.raises(ToolValidationError):
        registry.validate_arguments(
            "find_companies_missing_email",
            {"minimum_relevance": 50, "sql": "SELECT * FROM leads"},
        )


def test_find_companies_missing_email_matches_repository(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)
    store = SqliteContactStore(db_path)
    registry = build_default_registry()
    result = registry.execute(
        store,
        "find_companies_missing_email",
        {"missing_definition": "any"},
    )
    assert result.status == "ok"
    assert result.record_count == 1
    assert result.records[0]["company_name"] == "No Email Co"
    assert "missing_definition=any" in result.warnings[0]


def test_command_service_executes_read_tool_with_audit(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store)
    entry = service.receive("companies without email")
    service.plan_tool_call(
        entry,
        PlannerToolCall(
            tool_name="find_companies_missing_email",
            arguments={"missing_definition": "any"},
            reason="User asked for companies without email.",
        ),
    )
    result = service.execute_planned_tool(entry)
    assert result.record_count == 1
    assert result.command_id == entry.id
    updated = service.get_command(entry.id)
    assert updated is not None
    assert updated.status.value == "succeeded"
    assert updated.tool_name == "find_companies_missing_email"


def test_planner_model_rejects_extra_fields():
    with pytest.raises(ValidationError):
        PlannerToolCall(
            tool_name="search_contacts",
            arguments={"limit": 5},
            reason="test",
            sql="bad",
        )
