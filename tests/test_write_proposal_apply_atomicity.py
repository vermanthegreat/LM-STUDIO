"""Atomic apply and idempotent recovery for approved write proposals."""

from __future__ import annotations

from unittest.mock import patch
from uuid import UUID

import db
import pytest
from fastapi.testclient import TestClient

from app import create_app
from ask_router import execute_planner_propose_tool_route
from config import AppConfig
from repositories.sqlite_store import SqliteContactStore
from services.command_log import CommandStatus
from services.command_service import CommandService
from services.write_proposals import apply_write_proposal


def _seed(db_path):
    db.init_db(db_path)
    lead, _ = db.upsert_lead(
        {
            "company_name": "Acme Corp",
            "company_email": "hello@acme.example",
            "company_phone": "+1-555-0100",
            "website": "https://acme.example",
            "fit_score": 75,
        },
        db_path=db_path,
    )
    return lead["id"]


def _followup_payload(lead_id: int) -> dict:
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


def _contact_payload(lead_id: int, *, value: str = "sales@acme.example") -> dict:
    return {
        "action": "tool",
        "tool_name": "propose_contact_update",
        "arguments": {
            "lead_id": lead_id,
            "field": "company_email",
            "value": value,
        },
        "reason": "Planner proposed a contact update.",
    }


def _create_followup_proposal(service: CommandService, store: SqliteContactStore, lead_id: int) -> UUID:
    result = execute_planner_propose_tool_route(
        "create follow-up",
        _followup_payload(lead_id),
        store=store,
        command_service=service,
    )
    return UUID(result["data"]["command_id"])


def _create_contact_proposal(service: CommandService, store: SqliteContactStore, lead_id: int) -> UUID:
    result = execute_planner_propose_tool_route(
        "update email",
        _contact_payload(lead_id),
        store=store,
        command_service=service,
    )
    return UUID(result["data"]["command_id"])


def _task_titles(store: SqliteContactStore, lead_id: int) -> list[str]:
    lead = store.get_lead(lead_id)
    return [task["title"] for task in (lead.get("tasks") or [])] if lead else []


def test_followup_apply_idempotent_by_command_id(tmp_path):
    db_path = tmp_path / "test.db"
    lead_id = _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store)
    command_id = _create_followup_proposal(service, store, lead_id)

    service.approve_command(service.get_command(command_id))
    service.apply_approved_command(service.get_command(command_id))
    assert _task_titles(store, lead_id) == ["Check in on proposal"]

    entry = service.get_command(command_id)
    assert entry is not None
    with store.transaction():
        apply_write_proposal(store, entry)
    assert _task_titles(store, lead_id) == ["Check in on proposal"]


def test_simulated_terminal_update_failure_rolls_back_and_retry_is_safe(tmp_path):
    db_path = tmp_path / "test.db"
    lead_id = _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store)
    command_id = _create_followup_proposal(service, store, lead_id)
    service.approve_command(service.get_command(command_id))

    real_update = service.command_log.update

    def flaky_update(entry):
        if entry.status == CommandStatus.SUCCEEDED:
            raise RuntimeError("simulated command log failure")
        return real_update(entry)

    with patch.object(service.command_log, "update", side_effect=flaky_update):
        with pytest.raises(RuntimeError, match="simulated command log failure"):
            service.apply_approved_command(service.get_command(command_id))

    assert _task_titles(store, lead_id) == []
    reloaded = service.get_command(command_id)
    assert reloaded is not None
    assert reloaded.status == CommandStatus.AWAITING_APPROVAL

    service.apply_approved_command(reloaded)
    assert _task_titles(store, lead_id) == ["Check in on proposal"]
    final = service.get_command(command_id)
    assert final is not None
    assert final.status == CommandStatus.SUCCEEDED
    assert final.result_summary.get("applied_result") is not None


def test_executing_state_recovers_from_orphaned_task_without_duplicate(tmp_path):
    db_path = tmp_path / "test.db"
    lead_id = _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store)
    command_id = _create_followup_proposal(service, store, lead_id)
    service.approve_command(service.get_command(command_id))

    db.add_task(
        lead_id,
        {
            "title": "Check in on proposal",
            "status": "open",
            "created_by_command_id": str(command_id),
        },
        db_path=db_path,
    )

    entry = service.get_command(command_id)
    assert entry is not None
    entry.status = CommandStatus.EXECUTING
    service.command_log.update(entry)

    result = service.apply_approved_command(entry)
    assert result.record_count == 1
    assert _task_titles(store, lead_id) == ["Check in on proposal"]
    final = service.get_command(command_id)
    assert final is not None
    assert final.status == CommandStatus.SUCCEEDED


def test_contact_update_retry_is_idempotent(tmp_path):
    db_path = tmp_path / "test.db"
    lead_id = _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store)
    command_id = _create_contact_proposal(service, store, lead_id)

    service.approve_command(service.get_command(command_id))
    service.apply_approved_command(service.get_command(command_id))
    lead = store.get_lead(lead_id)
    assert lead is not None
    assert lead["company_email"] == "sales@acme.example"
    assert lead["company_phone"] == "+1-555-0100"
    assert lead["website"] == "https://acme.example"

    entry = service.get_command(command_id)
    assert entry is not None
    with store.transaction():
        apply_write_proposal(store, entry)
    lead = store.get_lead(lead_id)
    assert lead is not None
    assert lead["company_email"] == "sales@acme.example"
    assert lead["company_phone"] == "+1-555-0100"


def test_duplicate_apply_from_succeeded_still_returns_409(tmp_path):
    db_path = tmp_path / "routes.db"
    lead_id = _seed(db_path)
    cfg = AppConfig(database_path=db_path, max_paste_chars=1000, port=8025)
    client = TestClient(create_app(cfg), base_url="http://127.0.0.1:8025")
    store = SqliteContactStore(db_path)
    service = CommandService(store)
    command_id = _create_followup_proposal(service, store, lead_id)

    with client:
        client.post(f"/ask/commands/{command_id}/approve")
        first = client.post(f"/ask/commands/{command_id}/apply")
        assert first.status_code == 200
        second = client.post(f"/ask/commands/{command_id}/apply")
        assert second.status_code == 409
        assert second.json()["data"]["error_code"] == "CommandLogError"
        assert len(_task_titles(store, lead_id)) == 1
