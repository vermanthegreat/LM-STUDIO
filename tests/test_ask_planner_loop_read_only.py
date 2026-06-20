"""Tests for read-only planner execution path on /ask."""

from __future__ import annotations

from uuid import UUID

import db
import pytest
from pydantic import BaseModel, ConfigDict

from ask_router import answer_question, execute_planner_read_tool_route
from repositories.sqlite_store import SqliteContactStore
from services.command_log import CommandStatus, InMemoryCommandLog
from services.command_service import CommandService
from services.planner_validation import execute_planner_read_tool_call
from tools.envelope import ToolResult
from tools.registry import ToolSpec, build_default_registry
from tools.risk import RiskClass


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


class _WriteOnlyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str


def _write_only_handler(store, args: BaseModel) -> ToolResult:
    raise AssertionError("write tool must not execute in read-only planner path")


def _registry_with_write_tool():
    registry = build_default_registry()
    registry.register(
        ToolSpec(
            name="create_task",
            risk_class=RiskClass.WRITE,
            input_model=_WriteOnlyInput,
            handler=_write_only_handler,
            description="Write-only test stub.",
        )
    )
    return registry


def test_valid_planner_read_tool_executes(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=InMemoryCommandLog())

    result = execute_planner_read_tool_route(
        "companies without email",
        {
            "action": "tool",
            "tool_name": "find_companies_missing_email",
            "arguments": {"missing_definition": "any"},
            "reason": "Planner proposed read tool.",
        },
        store=store,
        command_service=service,
    )

    assert result["intent"] == "leads_without_email"
    assert result["data"]["record_count"] == 1
    assert "No Email Co" in result["answer"]
    entry = service.get_command(UUID(result["data"]["command_id"]))
    assert entry is not None
    assert entry.status == CommandStatus.SUCCEEDED


def test_unknown_planner_tool_rejected_before_execution(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=InMemoryCommandLog())

    result = execute_planner_read_tool_route(
        "unsafe",
        {
            "action": "tool",
            "tool_name": "run_sql",
            "arguments": {},
            "reason": "bad",
        },
        store=store,
        command_service=service,
    )

    assert result["intent"] == "tool_error"
    entry = service.get_command(UUID(result["data"]["command_id"]))
    assert entry.status == CommandStatus.REJECTED
    assert entry.error_code == "UnknownToolError"


def test_invalid_planner_arguments_rejected_before_execution(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=InMemoryCommandLog())

    result = execute_planner_read_tool_route(
        "bad args",
        {
            "action": "tool",
            "tool_name": "find_companies_missing_email",
            "arguments": {"missing_definition": "any", "sql": "DROP TABLE leads"},
            "reason": "bad",
        },
        store=store,
        command_service=service,
    )

    assert result["intent"] == "tool_error"
    entry = service.get_command(UUID(result["data"]["command_id"]))
    assert entry.status == CommandStatus.REJECTED
    assert entry.error_code == "ToolValidationError"


def test_non_read_tool_rejected_even_when_registered(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)
    store = SqliteContactStore(db_path)
    registry = _registry_with_write_tool()
    service = CommandService(store, registry=registry, command_log=InMemoryCommandLog())

    result = execute_planner_read_tool_call(
        service,
        command_text="create a task",
        payload={
            "action": "tool",
            "tool_name": "create_task",
            "arguments": {"title": "Follow up"},
            "reason": "write attempt",
        },
    )

    assert result["status"] == "error"
    assert result["intent"] == "tool_error"
    assert result["error_code"] == "non_read_tool_rejected"
    entry = service.get_command(UUID(result["data"]["command_id"]))
    assert entry.status == CommandStatus.REJECTED
    assert entry.error_code == "non_read_tool_rejected"


def test_answer_question_planner_payload_after_deterministic_routes(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)
    store = SqliteContactStore(db_path)

    deterministic = answer_question("companies without email", use_llm=False, store=store)
    assert deterministic["intent"] == "leads_without_email"

    legacy = answer_question("contact summary", use_llm=False, store=store)
    assert legacy["intent"] == "contact_summary"

    planner = answer_question(
        "contact summary",
        use_llm=False,
        store=store,
        planner_payload={
            "action": "tool",
            "tool_name": "calculate_pipeline_analytics",
            "arguments": {"metric": "organization_count"},
            "reason": "explicit planner seam",
        },
    )
    assert planner["intent"] == "calculate_pipeline_analytics"
    assert planner["data"]["record_count"] == 1
