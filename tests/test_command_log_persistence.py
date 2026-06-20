"""Tests for durable command_log persistence."""

from __future__ import annotations

import os
from uuid import UUID

import db
import pytest
from ask_router import execute_planner_read_tool_route
from repositories.command_log_store import PostgresCommandLogStore, SqliteCommandLogStore
from repositories.postgres_store import PostgresContactStore
from repositories.sqlite_store import SqliteContactStore
from services.command_log import CommandStatus
from services.command_service import CommandService
from services.planner_validation import validate_planner_tool_call

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")


def _seed_sqlite(db_path):
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


def test_sqlite_command_log_persists_across_service_instances(tmp_path):
    db_path = tmp_path / "test.db"
    _seed_sqlite(db_path)
    store = SqliteContactStore(db_path)

    service1 = CommandService(store)
    result = execute_planner_read_tool_route(
        "companies without email",
        {
            "action": "tool",
            "tool_name": "find_companies_missing_email",
            "arguments": {"missing_definition": "any"},
            "reason": "persist test",
        },
        store=store,
        command_service=service1,
    )
    command_id = UUID(result["data"]["command_id"])

    service2 = CommandService(SqliteContactStore(db_path))
    entry = service2.get_command(command_id)
    assert entry is not None
    assert entry.status == CommandStatus.SUCCEEDED
    assert entry.tool_name == "find_companies_missing_email"
    assert entry.tool_arguments is not None
    assert entry.tool_arguments["missing_definition"] == "any"
    assert entry.result_summary is not None


def test_sqlite_rejected_planner_attempt_persisted(tmp_path):
    db_path = tmp_path / "test.db"
    _seed_sqlite(db_path)
    store = SqliteContactStore(db_path)

    service1 = CommandService(store)
    result = validate_planner_tool_call(
        service1,
        command_text="bad planner",
        payload={
            "action": "tool",
            "tool_name": "run_sql",
            "arguments": {},
            "reason": "unsafe",
        },
    )
    command_id = UUID(result["data"]["command_id"])

    direct = SqliteCommandLogStore(db_path).get(command_id)
    service2 = CommandService(SqliteContactStore(db_path))
    entry = service2.get_command(command_id)

    assert direct is not None
    assert entry is not None
    assert direct.status == CommandStatus.REJECTED
    assert entry.status == CommandStatus.REJECTED
    assert entry.error_code == "UnknownToolError"


def test_sqlite_succeeded_read_tool_execution_persisted(tmp_path):
    db_path = tmp_path / "test.db"
    _seed_sqlite(db_path)
    store = SqliteContactStore(db_path)
    log_store = SqliteCommandLogStore(db_path)

    service = CommandService(store)
    result = execute_planner_read_tool_route(
        "follow ups",
        {
            "action": "tool",
            "tool_name": "list_due_followups",
            "arguments": {"limit": 10},
            "reason": "read tool",
        },
        store=store,
        command_service=service,
    )
    command_id = UUID(result["data"]["command_id"])
    row = log_store.get(command_id)

    assert row is not None
    assert row.status == CommandStatus.SUCCEEDED
    assert row.risk_class == "read"
    assert row.result_summary is not None


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not configured")
def test_postgres_command_log_persists_across_service_instances():
    store = PostgresContactStore(TEST_DATABASE_URL)
    service1 = CommandService(store)
    result = validate_planner_tool_call(
        service1,
        command_text="invalid planner payload",
        payload={
            "action": "tool",
            "tool_name": "find_companies_missing_email",
            "arguments": {"missing_definition": "any", "sql": "bad"},
            "reason": "bad args",
        },
    )
    command_id = UUID(result["data"]["command_id"])

    log_store = PostgresCommandLogStore(TEST_DATABASE_URL)
    service2 = CommandService(PostgresContactStore(TEST_DATABASE_URL))
    entry = service2.get_command(command_id)
    direct = log_store.get(command_id)

    assert entry is not None
    assert direct is not None
    assert entry.status == CommandStatus.REJECTED
    assert entry.error_code == "ToolValidationError"
