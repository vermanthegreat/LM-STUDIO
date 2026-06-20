"""Tests for Phase 2 /ask routing through typed read tools."""

from __future__ import annotations

from uuid import UUID

import db
import pytest
from ask_router import answer_question, execute_tool_route
from repositories.sqlite_store import SqliteContactStore
from services.command_log import CommandStatus, InMemoryCommandLog
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


def test_ask_routes_leads_without_email_through_registered_tool(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)
    log = InMemoryCommandLog()
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=log)

    result = answer_question(
        "companies without email",
        use_llm=False,
        store=store,
        command_service=service,
    )

    assert result["intent"] == "leads_without_email"
    assert result["data"]["tool_name"] == "find_companies_missing_email"
    assert result["data"]["record_count"] == 1
    assert "No Email Co" in result["answer"]

    entry = service.get_command(UUID(result["data"]["command_id"]))
    assert entry is not None
    assert entry.status == CommandStatus.SUCCEEDED
    assert entry.tool_name == "find_companies_missing_email"


def test_unknown_tool_rejected_with_typed_validation(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=InMemoryCommandLog())

    result = execute_tool_route(
        "run arbitrary sql",
        "run_sql",
        {"query": "SELECT * FROM leads"},
        store=store,
        command_service=service,
    )

    assert result["intent"] == "tool_error"
    entry = service.get_command(UUID(result["data"]["command_id"]))
    assert entry is not None
    assert entry.status == CommandStatus.REJECTED
    assert entry.error_code == "UnknownToolError"


def test_command_log_records_execution_path(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)
    log = InMemoryCommandLog()
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=log)

    result = execute_tool_route(
        "companies without email",
        "find_companies_missing_email",
        {"missing_definition": "any"},
        store=store,
        command_service=service,
    )

    entry = service.get_command(UUID(result["data"]["command_id"]))
    assert entry is not None
    assert entry.status == CommandStatus.SUCCEEDED
    assert entry.tool_arguments is not None
    assert entry.tool_arguments["missing_definition"] == "any"
    assert result["data"]["command_status"] == "succeeded"


def test_invalid_tool_arguments_rejected(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=InMemoryCommandLog())

    result = execute_tool_route(
        "bad args",
        "find_companies_missing_email",
        {"missing_definition": "any", "sql": "DROP TABLE leads"},
        store=store,
        command_service=service,
    )

    assert result["intent"] == "tool_error"
    entry = service.get_command(UUID(result["data"]["command_id"]))
    assert entry.status == CommandStatus.REJECTED
    assert entry.error_code == "ToolValidationError"
