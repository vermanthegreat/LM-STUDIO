"""Tests for planner tool-call output validation."""

from __future__ import annotations

from uuid import UUID

import db
from repositories.sqlite_store import SqliteContactStore
from services.command_log import CommandStatus, InMemoryCommandLog
from services.command_service import CommandService
from services.planner_validation import validate_planner_tool_call


def _store(tmp_path):
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    return SqliteContactStore(db_path)


def test_valid_planner_tool_call_accepted(tmp_path):
    store = _store(tmp_path)
    service = CommandService(store, command_log=InMemoryCommandLog())

    result = validate_planner_tool_call(
        service,
        command_text="find companies missing email",
        payload={
            "action": "tool",
            "tool_name": "find_companies_missing_email",
            "arguments": {"missing_definition": "any"},
            "reason": "User asked for companies without email.",
        },
    )

    assert result["status"] == "ok"
    assert result["intent"] == "planned"
    assert result["data"]["tool_name"] == "find_companies_missing_email"
    entry = service.get_command(UUID(result["data"]["command_id"]))
    assert entry is not None
    assert entry.status == CommandStatus.PLANNED
    assert entry.tool_arguments is not None
    assert entry.tool_arguments["missing_definition"] == "any"


def test_planner_payload_extra_field_rejected(tmp_path):
    store = _store(tmp_path)
    service = CommandService(store, command_log=InMemoryCommandLog())

    result = validate_planner_tool_call(
        service,
        command_text="bad planner output",
        payload={
            "action": "tool",
            "tool_name": "find_companies_missing_email",
            "arguments": {"missing_definition": "any"},
            "reason": "test",
            "sql": "SELECT 1",
        },
    )

    assert result["status"] == "error"
    assert result["intent"] == "planner_validation_error"
    assert result["error_code"] == "planner_validation_error"
    entry = service.get_command(UUID(result["data"]["command_id"]))
    assert entry is not None
    assert entry.status == CommandStatus.REJECTED
    assert entry.error_code == "planner_validation_error"


def test_unknown_tool_rejected(tmp_path):
    store = _store(tmp_path)
    service = CommandService(store, command_log=InMemoryCommandLog())

    result = validate_planner_tool_call(
        service,
        command_text="run sql",
        payload={
            "action": "tool",
            "tool_name": "run_sql",
            "arguments": {},
            "reason": "unsafe",
        },
    )

    assert result["status"] == "error"
    assert result["intent"] == "tool_error"
    assert result["error_code"] == "UnknownToolError"
    entry = service.get_command(UUID(result["data"]["command_id"]))
    assert entry.status == CommandStatus.REJECTED
    assert entry.error_code == "UnknownToolError"


def test_tool_arguments_extra_field_rejected(tmp_path):
    store = _store(tmp_path)
    service = CommandService(store, command_log=InMemoryCommandLog())

    result = validate_planner_tool_call(
        service,
        command_text="bad args",
        payload={
            "action": "tool",
            "tool_name": "find_companies_missing_email",
            "arguments": {"missing_definition": "any", "sql": "DROP TABLE leads"},
            "reason": "test",
        },
    )

    assert result["status"] == "error"
    assert result["intent"] == "tool_error"
    assert result["error_code"] == "ToolValidationError"
    entry = service.get_command(UUID(result["data"]["command_id"]))
    assert entry.status == CommandStatus.REJECTED
    assert entry.error_code == "ToolValidationError"


def test_rejected_planner_attempt_recorded_in_command_log(tmp_path):
    store = _store(tmp_path)
    log = InMemoryCommandLog()
    service = CommandService(store, command_log=log)

    result = validate_planner_tool_call(
        service,
        command_text="invalid planner",
        payload={"action": "tool", "tool_name": "missing_tool", "arguments": {}},
    )

    assert result["status"] == "error"
    assert len(log._entries) == 1
    entry = service.get_command(UUID(result["data"]["command_id"]))
    assert entry.command_text == "invalid planner"
    assert entry.status == CommandStatus.REJECTED
    assert entry.error_message
