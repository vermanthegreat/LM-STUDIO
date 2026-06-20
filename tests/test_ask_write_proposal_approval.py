"""Tests for /ask write-proposal preview and approval/apply routes."""

from __future__ import annotations

from uuid import UUID

import db
import pytest
from fastapi.testclient import TestClient

from ask_router import (
    answer_question,
    apply_write_proposal_route,
    approve_write_proposal_route,
    execute_planner_propose_tool_route,
)
from config import AppConfig
from app import create_app
from repositories.sqlite_store import SqliteContactStore
from services.command_log import CommandLogError, CommandStatus, InMemoryCommandLog
from services.command_service import CommandService


def _seed(db_path):
    db.init_db(db_path)
    lead, _ = db.upsert_lead(
        {
            "company_name": "Acme Corp",
            "company_email": "hello@acme.example",
            "fit_score": 75,
        },
        db_path=db_path,
    )
    return lead["id"]


def _proposal_payload(lead_id: int) -> dict:
    return {
        "action": "tool",
        "tool_name": "propose_create_followup",
        "arguments": {
            "lead_id": lead_id,
            "title": "Check in on proposal",
            "due_date": "2026-06-23",
            "priority": "normal",
        },
        "reason": "Planner proposed a follow-up task.",
    }


def _task_count(store: SqliteContactStore, lead_id: int) -> int:
    lead = store.get_lead(lead_id)
    return len(lead.get("tasks") or []) if lead else 0


def test_ask_planner_proposal_returns_awaiting_approval(tmp_path):
    db_path = tmp_path / "test.db"
    lead_id = _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=InMemoryCommandLog())

    result = answer_question(
        "create a follow-up for Acme",
        use_llm=False,
        store=store,
        command_service=service,
        planner_payload=_proposal_payload(lead_id),
    )

    assert result["intent"] == "write_proposal"
    assert result["data"]["command_status"] == CommandStatus.AWAITING_APPROVAL.value
    assert result["data"]["tool_name"] == "propose_create_followup"
    assert result["data"]["proposal"]["lead_id"] == lead_id
    assert result["data"]["record_count"] == 0
    assert "No changes have been made" in result["answer"]
    assert _task_count(store, lead_id) == 0

    entry = service.get_command(UUID(result["data"]["command_id"]))
    assert entry is not None
    assert entry.status == CommandStatus.AWAITING_APPROVAL
    assert entry.result_summary is not None
    assert entry.result_summary["proposal"]["title"] == "Check in on proposal"


def test_unapproved_proposal_cannot_apply(tmp_path):
    db_path = tmp_path / "test.db"
    lead_id = _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=InMemoryCommandLog())

    result = execute_planner_propose_tool_route(
        "create follow-up",
        _proposal_payload(lead_id),
        store=store,
        command_service=service,
    )
    command_id = UUID(result["data"]["command_id"])

    apply_result = apply_write_proposal_route(command_id, store=store, command_service=service)
    assert apply_result["status"] == "error"
    assert apply_result["data"]["error_code"] == "CommandLogError"
    assert _task_count(store, lead_id) == 0


def test_approve_and_apply_creates_one_task(tmp_path):
    db_path = tmp_path / "test.db"
    lead_id = _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=InMemoryCommandLog())

    result = execute_planner_propose_tool_route(
        "create follow-up",
        _proposal_payload(lead_id),
        store=store,
        command_service=service,
    )
    command_id = UUID(result["data"]["command_id"])

    approve_result = approve_write_proposal_route(command_id, store=store, command_service=service)
    assert approve_result["status"] == "ok"
    assert approve_result["intent"] == "write_proposal_approved"

    apply_result = apply_write_proposal_route(command_id, store=store, command_service=service)
    assert apply_result["status"] == "ok"
    assert apply_result["intent"] == "write_applied"
    assert apply_result["data"]["record_count"] == 1
    assert apply_result["data"]["command_status"] == CommandStatus.SUCCEEDED.value
    assert _task_count(store, lead_id) == 1

    entry = service.get_command(command_id)
    assert entry is not None
    assert entry.status == CommandStatus.SUCCEEDED
    assert entry.result_summary is not None
    assert "applied_result" in entry.result_summary


def test_duplicate_apply_is_rejected(tmp_path):
    db_path = tmp_path / "test.db"
    lead_id = _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=InMemoryCommandLog())

    result = execute_planner_propose_tool_route(
        "create follow-up",
        _proposal_payload(lead_id),
        store=store,
        command_service=service,
    )
    command_id = UUID(result["data"]["command_id"])
    approve_write_proposal_route(command_id, store=store, command_service=service)
    apply_write_proposal_route(command_id, store=store, command_service=service)

    duplicate = apply_write_proposal_route(command_id, store=store, command_service=service)
    assert duplicate["status"] == "error"
    assert duplicate["data"]["error_code"] == "CommandLogError"
    assert "already been applied" in duplicate["answer"]
    assert _task_count(store, lead_id) == 1


def test_http_approve_and_apply_routes(tmp_path):
    db_path = tmp_path / "routes.db"
    lead_id = _seed(db_path)
    cfg = AppConfig(database_path=db_path, max_paste_chars=1000, port=8025)
    client = TestClient(create_app(cfg), base_url="http://127.0.0.1:8025")
    store = SqliteContactStore(db_path)
    service = CommandService(store)

    with client:
        proposal = answer_question(
            "create follow-up",
            use_llm=False,
            store=store,
            command_service=service,
            planner_payload=_proposal_payload(lead_id),
        )
        command_id = proposal["data"]["command_id"]
        assert _task_count(store, lead_id) == 0

        approve = client.post(f"/ask/commands/{command_id}/approve")
        assert approve.status_code == 200
        assert approve.json()["status"] == "ok"

        apply = client.post(f"/ask/commands/{command_id}/apply")
        assert apply.status_code == 200
        body = apply.json()
        assert body["status"] == "ok"
        assert body["intent"] == "write_applied"
        assert _task_count(store, lead_id) == 1

        again = client.post(f"/ask/commands/{command_id}/apply")
        assert again.status_code == 409
        assert again.json()["status"] == "error"


def test_service_apply_without_approval_raises(tmp_path):
    db_path = tmp_path / "test.db"
    lead_id = _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=InMemoryCommandLog())

    result = execute_planner_propose_tool_route(
        "create follow-up",
        _proposal_payload(lead_id),
        store=store,
        command_service=service,
    )
    entry = service.get_command(UUID(result["data"]["command_id"]))
    assert entry is not None

    with pytest.raises(CommandLogError, match="not been approved"):
        service.apply_approved_command(entry)
