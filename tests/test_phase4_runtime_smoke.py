"""Phase 4 runtime smoke: governed HTTP operator flow through the app."""

from __future__ import annotations

from uuid import UUID

import db
from fastapi.testclient import TestClient

from app import create_app
from config import AppConfig
from repositories.sqlite_store import SqliteContactStore
from services.command_log import CommandStatus
from services.command_service import CommandService


def _seed_runtime_fixture(db_path):
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


def test_runtime_smoke_governed_ask_workflow(tmp_path):
    db_path = tmp_path / "runtime_smoke.db"
    lead_id = _seed_runtime_fixture(db_path)
    cfg = AppConfig(database_path=db_path, max_paste_chars=1000, port=8025)
    client = TestClient(create_app(cfg), base_url="http://127.0.0.1:8025")
    store = SqliteContactStore(db_path)
    service = CommandService(store)

    with client:
        read_resp = client.post(
            "/ask",
            json={
                "question": "what follow-ups are due",
                "planner_payload": {
                    "action": "tool",
                    "tool_name": "list_due_followups",
                    "arguments": {"limit": 10},
                    "reason": "Runtime smoke read.",
                },
            },
        )
        assert read_resp.status_code == 200
        read_body = read_resp.json()
        assert read_body["intent"] == "followups_due"
        assert read_body["data"]["record_count"] >= 1
        due_items = read_body["data"]["items"]
        assert any(item.get("company_name") == "Pipeline Co" for item in due_items)

        read_command_id = UUID(read_body["data"]["command_id"])
        read_entry = service.get_command(read_command_id)
        assert read_entry is not None
        assert read_entry.status == CommandStatus.SUCCEEDED

        propose_resp = client.post(
            "/ask",
            json={
                "question": "schedule check-in for Pipeline Co",
                "planner_payload": {
                    "action": "tool",
                    "tool_name": "propose_create_followup",
                    "arguments": {
                        "lead_id": lead_id,
                        "title": "Check in after renewal",
                        "due_date": "2026-06-25",
                        "priority": "normal",
                    },
                    "reason": "Runtime smoke propose.",
                },
            },
        )
        assert propose_resp.status_code == 200
        propose_body = propose_resp.json()
        assert propose_body["intent"] == "write_proposal"
        assert propose_body["data"]["command_status"] == CommandStatus.AWAITING_APPROVAL.value
        command_id = propose_body["data"]["command_id"]
        assert _task_titles(store, lead_id) == ["Renew contract"]

        pending = client.get("/ask/commands/pending")
        assert pending.status_code == 200
        pending_body = pending.json()
        assert pending_body["data"]["count"] == 1
        assert pending_body["data"]["proposals"][0]["command_id"] == command_id
        assert pending_body["data"]["proposals"][0]["tool_name"] == "propose_create_followup"

        detail = client.get(f"/ask/commands/{command_id}")
        assert detail.status_code == 200
        detail_body = detail.json()
        assert detail_body["data"]["command_id"] == command_id
        assert detail_body["data"]["tool_name"] == "propose_create_followup"
        assert detail_body["data"]["proposal"]["title"] == "Check in after renewal"
        assert detail_body["data"]["proposal"]["lead_id"] == lead_id

        approve = client.post(f"/ask/commands/{command_id}/approve")
        assert approve.status_code == 200
        approve_body = approve.json()
        assert approve_body["status"] == "ok"
        assert approve_body["intent"] == "write_proposal_approved"

        apply = client.post(f"/ask/commands/{command_id}/apply")
        assert apply.status_code == 200
        apply_body = apply.json()
        assert apply_body["status"] == "ok"
        assert apply_body["intent"] == "write_applied"
        assert apply_body["data"]["record_count"] == 1
        assert apply_body["data"]["command_status"] == CommandStatus.SUCCEEDED.value

        assert _task_titles(store, lead_id) == ["Renew contract", "Check in after renewal"]

        applied_entry = service.get_command(UUID(command_id))
        assert applied_entry is not None
        assert applied_entry.status == CommandStatus.SUCCEEDED

        pending_after = client.get("/ask/commands/pending")
        assert pending_after.status_code == 200
        assert pending_after.json()["data"]["count"] == 0

        again = client.post(f"/ask/commands/{command_id}/apply")
        assert again.status_code == 409
        assert again.json()["status"] == "error"
