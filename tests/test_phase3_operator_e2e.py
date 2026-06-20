"""End-to-end operator workflow: read → propose → review → approve → apply."""

from __future__ import annotations

from uuid import UUID

import db
from ask_router import (
    apply_write_proposal_route,
    approve_write_proposal_route,
    execute_planner_propose_tool_route,
    execute_planner_read_tool_route,
    get_write_proposal_detail_route,
    list_pending_write_proposals_route,
)
from repositories.sqlite_store import SqliteContactStore
from services.command_log import CommandStatus, InMemoryCommandLog
from services.command_service import CommandService


def _seed_operator_fixture(db_path):
    db.init_db(db_path)
    lead, _ = db.upsert_lead(
        {
            "company_name": "Pipeline Co",
            "fit_score": 70,
        },
        db_path=db_path,
    )
    db.add_person(
        lead["id"],
        {"name": "Alex Owner", "title": "CEO", "is_decision_maker": 1},
        db_path=db_path,
    )
    db.add_task(
        lead["id"],
        {"title": "Renew contract", "due_date": "2020-01-01", "status": "open"},
        db_path=db_path,
    )
    return lead["id"]


def _task_titles(store: SqliteContactStore, lead_id: int) -> list[str]:
    lead = store.get_lead(lead_id)
    tasks = lead.get("tasks") or [] if lead else []
    return [task["title"] for task in tasks]


def test_operator_read_propose_approve_apply_e2e(tmp_path):
    db_path = tmp_path / "operator_e2e.db"
    lead_id = _seed_operator_fixture(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=InMemoryCommandLog())

    read_result = execute_planner_read_tool_route(
        "what follow-ups are due",
        {
            "action": "tool",
            "tool_name": "list_due_followups",
            "arguments": {"limit": 10},
            "reason": "Operator reviewing due follow-ups.",
        },
        store=store,
        command_service=service,
    )
    assert read_result["intent"] == "followups_due"
    assert read_result["data"]["record_count"] >= 1
    due_items = read_result["data"]["items"]
    assert any(item.get("company_name") == "Pipeline Co" for item in due_items)
    assert any(item.get("title") == "Renew contract" for item in due_items)

    read_entry = service.get_command(UUID(read_result["data"]["command_id"]))
    assert read_entry is not None
    assert read_entry.status == CommandStatus.SUCCEEDED

    proposal_result = execute_planner_propose_tool_route(
        "schedule check-in for Pipeline Co",
        {
            "action": "tool",
            "tool_name": "propose_create_followup",
            "arguments": {
                "lead_id": lead_id,
                "title": "Check in after renewal",
                "due_date": "2026-06-25",
                "priority": "normal",
            },
            "reason": "Follow up on due renewal task.",
        },
        store=store,
        command_service=service,
    )
    assert proposal_result["intent"] == "write_proposal"
    assert proposal_result["data"]["command_status"] == CommandStatus.AWAITING_APPROVAL.value
    command_id = UUID(proposal_result["data"]["command_id"])
    assert _task_titles(store, lead_id) == ["Renew contract"]

    pending = list_pending_write_proposals_route(store=store, command_service=service)
    assert pending["status"] == "ok"
    assert pending["data"]["count"] == 1
    assert pending["data"]["proposals"][0]["command_id"] == str(command_id)
    assert pending["data"]["proposals"][0]["tool_name"] == "propose_create_followup"

    detail = get_write_proposal_detail_route(command_id, store=store, command_service=service)
    assert detail["status"] == "ok"
    assert detail["intent"] == "write_proposal_detail"
    assert detail["data"]["status"] == CommandStatus.AWAITING_APPROVAL.value
    assert detail["data"]["proposal"]["title"] == "Check in after renewal"
    assert detail["data"]["proposal"]["lead_id"] == lead_id

    approve_result = approve_write_proposal_route(command_id, store=store, command_service=service)
    assert approve_result["status"] == "ok"
    assert approve_result["intent"] == "write_proposal_approved"

    apply_result = apply_write_proposal_route(command_id, store=store, command_service=service)
    assert apply_result["status"] == "ok"
    assert apply_result["intent"] == "write_applied"
    assert apply_result["data"]["record_count"] == 1
    assert apply_result["data"]["command_status"] == CommandStatus.SUCCEEDED.value

    assert _task_titles(store, lead_id) == ["Renew contract", "Check in after renewal"]

    applied_entry = service.get_command(command_id)
    assert applied_entry is not None
    assert applied_entry.status == CommandStatus.SUCCEEDED
    assert applied_entry.result_summary is not None
    assert "applied_result" in applied_entry.result_summary

    pending_after = list_pending_write_proposals_route(store=store, command_service=service)
    assert pending_after["data"]["count"] == 0
