"""Authority, replay, and ownership checks for write proposal approve/apply."""

from __future__ import annotations

from uuid import UUID, uuid4

import db
import pytest
from fastapi.testclient import TestClient

from app import create_app
from ask_router import apply_write_proposal_route, approve_write_proposal_route, execute_planner_propose_tool_route
from config import AppConfig
from repositories.sqlite_store import SqliteContactStore
from services.command_log import CommandLogError, CommandStatus, InMemoryCommandLog, transition
from services.command_service import CommandService
from tools.planner import PlannerToolCall


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


def _create_proposal(service: CommandService, store: SqliteContactStore, lead_id: int) -> UUID:
    result = execute_planner_propose_tool_route(
        "create follow-up",
        _proposal_payload(lead_id),
        store=store,
        command_service=service,
    )
    return UUID(result["data"]["command_id"])


def _task_titles(store: SqliteContactStore, lead_id: int) -> list[str]:
    lead = store.get_lead(lead_id)
    return [task["title"] for task in (lead.get("tasks") or [])] if lead else []


def test_unknown_command_id_rejected_for_approve_and_apply(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=InMemoryCommandLog())
    missing_id = uuid4()

    approve = approve_write_proposal_route(missing_id, store=store, command_service=service)
    assert approve["status"] == "error"
    assert approve["data"]["error_code"] == "command_not_found"

    apply = apply_write_proposal_route(missing_id, store=store, command_service=service)
    assert apply["status"] == "error"
    assert apply["data"]["error_code"] == "command_not_found"


def test_apply_before_approval_rejected(tmp_path):
    db_path = tmp_path / "test.db"
    lead_id = _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=InMemoryCommandLog())
    command_id = _create_proposal(service, store, lead_id)

    result = apply_write_proposal_route(command_id, store=store, command_service=service)
    assert result["status"] == "error"
    assert result["data"]["error_code"] == "CommandLogError"
    assert "not been approved" in result["answer"]
    assert _task_titles(store, lead_id) == []


def test_approve_after_succeeded_or_failed_rejected(tmp_path):
    db_path = tmp_path / "test.db"
    lead_id = _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=InMemoryCommandLog())
    command_id = _create_proposal(service, store, lead_id)

    approve_write_proposal_route(command_id, store=store, command_service=service)
    apply_write_proposal_route(command_id, store=store, command_service=service)

    after_success = approve_write_proposal_route(command_id, store=store, command_service=service)
    assert after_success["status"] == "error"
    assert "already been applied" in after_success["answer"]

    failed_id = _create_proposal(service, store, lead_id)
    failed_entry = service.get_command(failed_id)
    assert failed_entry is not None
    transition(failed_entry, CommandStatus.FAILED)
    failed_entry.error_code = "WriteProposalError"
    service.command_log.update(failed_entry)
    after_failed = approve_write_proposal_route(failed_id, store=store, command_service=service)
    assert after_failed["status"] == "error"
    assert "failed state" in after_failed["answer"]


def test_approve_rejects_missing_or_malformed_proposal(tmp_path):
    db_path = tmp_path / "test.db"
    lead_id = _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=InMemoryCommandLog())

    missing = _create_proposal(service, store, lead_id)
    entry = service.get_command(missing)
    assert entry is not None
    entry.result_summary = {"reason": "only"}
    service.command_log.update(entry)
    missing_approve = approve_write_proposal_route(missing, store=store, command_service=service)
    assert missing_approve["status"] == "error"
    assert "stored write proposal" in missing_approve["answer"].lower()


def test_apply_rejects_missing_or_malformed_proposal(tmp_path):
    db_path = tmp_path / "test.db"
    lead_id = _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=InMemoryCommandLog())

    missing = _create_proposal(service, store, lead_id)
    entry = service.get_command(missing)
    assert entry is not None
    entry.approved_at = entry.updated_at
    entry.result_summary = {"reason": "only"}
    service.command_log.update(entry)
    missing_apply = apply_write_proposal_route(missing, store=store, command_service=service)
    assert missing_apply["status"] == "error"
    assert "stored write proposal" in missing_apply["answer"].lower()

    malformed = _create_proposal(service, store, lead_id)
    bad_entry = service.get_command(malformed)
    assert bad_entry is not None
    bad_entry.approved_at = bad_entry.updated_at
    bad_entry.result_summary = {
        "reason": "x",
        "proposal": {"action": "create_followup", "lead_id": lead_id, "title": ""},
    }
    service.command_log.update(bad_entry)
    malformed_apply = apply_write_proposal_route(malformed, store=store, command_service=service)
    assert malformed_apply["status"] == "error"
    assert "missing lead_id or title" in malformed_apply["answer"].lower()
    assert _task_titles(store, lead_id) == []


def test_apply_uses_persisted_command_log_proposal_not_request_body(tmp_path):
    db_path = tmp_path / "routes.db"
    lead_id = _seed(db_path)
    cfg = AppConfig(database_path=db_path, max_paste_chars=1000, port=8025)
    client = TestClient(create_app(cfg), base_url="http://127.0.0.1:8025")
    store = SqliteContactStore(db_path)
    service = CommandService(store)
    command_id = _create_proposal(service, store, lead_id)

    with client:
        approve = client.post(f"/ask/commands/{command_id}/approve")
        assert approve.status_code == 200

        apply = client.post(
            f"/ask/commands/{command_id}/apply",
            json={
                "proposal": {
                    "action": "create_followup",
                    "lead_id": lead_id,
                    "title": "Injected title must be ignored",
                }
            },
        )
        assert apply.status_code == 200
        assert _task_titles(store, lead_id) == ["Check in on proposal"]


def test_apply_reloads_authoritative_command_log_over_stale_memory(tmp_path):
    db_path = tmp_path / "test.db"
    lead_id = _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store)
    command_id = _create_proposal(service, store, lead_id)

    stale = service.get_command(command_id)
    assert stale is not None
    stale.result_summary = {
        **stale.result_summary,
        "proposal": {
            **stale.result_summary["proposal"],
            "title": "Stale in-memory title",
        },
    }

    approve_write_proposal_route(command_id, store=store, command_service=service)
    result = apply_write_proposal_route(command_id, store=store, command_service=service)
    assert result["status"] == "ok"
    assert _task_titles(store, lead_id) == ["Check in on proposal"]


def test_duplicate_apply_returns_409(tmp_path):
    db_path = tmp_path / "routes.db"
    lead_id = _seed(db_path)
    cfg = AppConfig(database_path=db_path, max_paste_chars=1000, port=8025)
    client = TestClient(create_app(cfg), base_url="http://127.0.0.1:8025")
    store = SqliteContactStore(db_path)
    service = CommandService(store)
    command_id = _create_proposal(service, store, lead_id)

    with client:
        client.post(f"/ask/commands/{command_id}/approve")
        first = client.post(f"/ask/commands/{command_id}/apply")
        assert first.status_code == 200
        second = client.post(f"/ask/commands/{command_id}/apply")
        assert second.status_code == 409
        assert second.json()["data"]["error_code"] == "CommandLogError"
        assert len(_task_titles(store, lead_id)) == 1


def test_service_reload_raises_for_missing_command(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=InMemoryCommandLog())
    ghost = service.receive("ghost")
    ghost_id = ghost.id
    service.command_log._entries.pop(ghost_id, None)

    with pytest.raises(CommandLogError, match="was not found"):
        service.approve_command(ghost)
