"""Tests for Phase 3 controlled write proposals."""

from __future__ import annotations

from uuid import UUID

import db
import pytest
from services.command_log import CommandLogError, CommandStatus, InMemoryCommandLog
from services.command_service import CommandService
from services.planner_validation import validate_planner_tool_call
from tools.planner import PlannerToolCall
from tools.registry import ToolValidationError, build_default_registry
from tools.write_handlers import WriteProposalError
from repositories.sqlite_store import SqliteContactStore


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


def _propose_followup(service: CommandService, lead_id: int):
    entry = service.receive("create follow-up for Acme")
    service.plan_tool_call(
        entry,
        PlannerToolCall(
            tool_name="propose_create_followup",
            arguments={
                "lead_id": lead_id,
                "title": "Check in on proposal",
                "due_date": "2026-06-23",
                "priority": "normal",
            },
            reason="User asked for a follow-up task.",
        ),
    )
    return entry, service.execute_planned_tool(entry)


def test_propose_create_followup_creates_proposal(tmp_path):
    db_path = tmp_path / "test.db"
    lead_id = _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=InMemoryCommandLog())

    entry, result = _propose_followup(service, lead_id)

    assert result.status == "ok"
    assert result.proposal is not None
    assert result.proposal["action"] == "create_followup"
    assert result.proposal["lead_id"] == lead_id
    assert result.proposal["title"] == "Check in on proposal"
    assert result.command_id == entry.id

    updated = service.get_command(entry.id)
    assert updated is not None
    assert updated.status == CommandStatus.AWAITING_APPROVAL
    assert updated.requires_approval is True
    assert updated.result_summary is not None
    assert updated.result_summary["proposal"]["title"] == "Check in on proposal"

    lead = store.get_lead(lead_id)
    assert lead is not None
    assert not lead.get("tasks")


def test_unapproved_write_cannot_apply(tmp_path):
    db_path = tmp_path / "test.db"
    lead_id = _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=InMemoryCommandLog())

    entry, _ = _propose_followup(service, lead_id)

    with pytest.raises(CommandLogError, match="not been approved"):
        service.apply_approved_command(entry)

    lead = store.get_lead(lead_id)
    assert not lead.get("tasks")


def test_approved_write_applies_task(tmp_path):
    db_path = tmp_path / "test.db"
    lead_id = _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=InMemoryCommandLog())

    entry, _ = _propose_followup(service, lead_id)
    service.approve_command(entry)

    result = service.apply_approved_command(entry)
    assert result.status == "ok"
    assert result.record_count == 1
    assert result.records[0]["title"] == "Check in on proposal"

    updated = service.get_command(entry.id)
    assert updated is not None
    assert updated.status == CommandStatus.SUCCEEDED
    assert updated.approved_at is not None

    lead = store.get_lead(lead_id)
    assert len(lead.get("tasks") or []) == 1
    assert lead["tasks"][0]["title"] == "Check in on proposal"
    assert lead["tasks"][0]["due_date"] == "2026-06-23"


def test_rejected_bad_args_logged(tmp_path):
    db_path = tmp_path / "test.db"
    lead_id = _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=InMemoryCommandLog())

    validation = validate_planner_tool_call(
        service,
        command_text="bad follow-up",
        payload={
            "action": "tool",
            "tool_name": "propose_create_followup",
            "arguments": {"lead_id": lead_id, "title": "", "extra": "nope"},
            "reason": "invalid title",
        },
    )
    assert validation["status"] == "error"
    entry = service.get_command(UUID(validation["data"]["command_id"]))
    assert entry is not None
    assert entry.status == CommandStatus.REJECTED
    assert entry.error_code == "ToolValidationError"


def test_missing_lead_proposal_fails_and_logs(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=InMemoryCommandLog())

    entry = service.receive("follow-up for missing lead")
    service.plan_tool_call(
        entry,
        PlannerToolCall(
            tool_name="propose_create_followup",
            arguments={"lead_id": 9999, "title": "Unreachable"},
            reason="bad target",
        ),
    )

    with pytest.raises(WriteProposalError, match="Lead not found"):
        service.execute_planned_tool(entry)

    updated = service.get_command(entry.id)
    assert updated is not None
    assert updated.status == CommandStatus.FAILED
    assert updated.error_code == "WriteProposalError"


def test_write_proposal_persists_in_sqlite_command_log(tmp_path):
    db_path = tmp_path / "test.db"
    lead_id = _seed(db_path)
    store = SqliteContactStore(db_path)

    service1 = CommandService(store)
    entry, _ = _propose_followup(service1, lead_id)

    service2 = CommandService(SqliteContactStore(db_path))
    loaded = service2.get_command(entry.id)
    assert loaded is not None
    assert loaded.status == CommandStatus.AWAITING_APPROVAL
    assert loaded.result_summary["proposal"]["lead_id"] == lead_id


def test_unknown_proposal_fields_rejected_by_registry():
    registry = build_default_registry()
    with pytest.raises(ToolValidationError):
        registry.validate_arguments(
            "propose_create_followup",
            {"lead_id": 1, "title": "Follow up", "sql": "DROP TABLE leads"},
        )
