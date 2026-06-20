"""Tests for operator-facing write proposal listing and detail."""

from __future__ import annotations

from uuid import UUID, uuid4

import db
from fastapi.testclient import TestClient

from app import create_app
from ask_router import (
    apply_write_proposal_route,
    approve_write_proposal_route,
    execute_planner_propose_tool_route,
    get_write_proposal_detail_route,
    list_pending_write_proposals_route,
)
from config import AppConfig
from repositories.sqlite_store import SqliteContactStore
from services.command_log import CommandStatus, InMemoryCommandLog
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


def _followup_payload(lead_id: int) -> dict:
    return {
        "action": "tool",
        "tool_name": "propose_create_followup",
        "arguments": {
            "lead_id": lead_id,
            "title": "Check in on proposal",
            "due_date": "2026-06-23",
        },
        "reason": "Planner proposed a follow-up task.",
    }


def _contact_payload(lead_id: int) -> dict:
    return {
        "action": "tool",
        "tool_name": "propose_contact_update",
        "arguments": {
            "lead_id": lead_id,
            "field": "company_email",
            "value": "sales@acme.example",
        },
        "reason": "Planner proposed a contact update.",
    }


def test_pending_proposals_are_listable(tmp_path):
    db_path = tmp_path / "test.db"
    lead_id = _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=InMemoryCommandLog())

    followup = execute_planner_propose_tool_route(
        "follow up",
        _followup_payload(lead_id),
        store=store,
        command_service=service,
    )
    contact = execute_planner_propose_tool_route(
        "update email",
        _contact_payload(lead_id),
        store=store,
        command_service=service,
    )

    result = list_pending_write_proposals_route(store=store, command_service=service)
    assert result["status"] == "ok"
    assert result["intent"] == "write_proposals_pending"
    assert result["data"]["count"] == 2
    tool_names = {item["tool_name"] for item in result["data"]["proposals"]}
    assert tool_names == {"propose_create_followup", "propose_contact_update"}
    ids = {item["command_id"] for item in result["data"]["proposals"]}
    assert followup["data"]["command_id"] in ids
    assert contact["data"]["command_id"] in ids


def test_succeeded_and_rejected_commands_not_listed_as_pending(tmp_path):
    db_path = tmp_path / "test.db"
    lead_id = _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=InMemoryCommandLog())

    applied = execute_planner_propose_tool_route(
        "follow up",
        _followup_payload(lead_id),
        store=store,
        command_service=service,
    )
    applied_id = UUID(applied["data"]["command_id"])
    approve_write_proposal_route(applied_id, store=store, command_service=service)
    apply_write_proposal_route(applied_id, store=store, command_service=service)

    rejected = execute_planner_propose_tool_route(
        "update email",
        _contact_payload(lead_id),
        store=store,
        command_service=service,
    )
    rejected_id = UUID(rejected["data"]["command_id"])
    entry = service.get_command(rejected_id)
    assert entry is not None
    service.reject(entry, code="operator_rejected", message="Not now")

    pending = list_pending_write_proposals_route(store=store, command_service=service)
    assert pending["data"]["count"] == 0


def test_proposal_detail_shows_operator_fields(tmp_path):
    db_path = tmp_path / "test.db"
    lead_id = _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=InMemoryCommandLog())

    proposal = execute_planner_propose_tool_route(
        "follow up",
        _followup_payload(lead_id),
        store=store,
        command_service=service,
    )
    command_id = UUID(proposal["data"]["command_id"])

    detail = get_write_proposal_detail_route(command_id, store=store, command_service=service)
    assert detail["status"] == "ok"
    assert detail["intent"] == "write_proposal_detail"
    data = detail["data"]
    assert data["command_id"] == str(command_id)
    assert data["status"] == CommandStatus.AWAITING_APPROVAL.value
    assert data["tool_name"] == "propose_create_followup"
    assert data["summary"]
    assert data["proposal"]["title"] == "Check in on proposal"
    assert data["created_at"]
    assert data["approved_at"] is None


def test_detail_rejects_unknown_command_id(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=InMemoryCommandLog())
    missing = uuid4()

    detail = get_write_proposal_detail_route(missing, store=store, command_service=service)
    assert detail["status"] == "error"
    assert detail["data"]["error_code"] == "command_not_found"


def test_http_pending_and_detail_routes(tmp_path):
    db_path = tmp_path / "routes.db"
    lead_id = _seed(db_path)
    cfg = AppConfig(database_path=db_path, max_paste_chars=1000, port=8025)
    client = TestClient(create_app(cfg), base_url="http://127.0.0.1:8025")
    store = SqliteContactStore(db_path)
    service = CommandService(store)

    proposal = execute_planner_propose_tool_route(
        "follow up",
        _followup_payload(lead_id),
        store=store,
        command_service=service,
    )
    command_id = proposal["data"]["command_id"]

    with client:
        pending = client.get("/ask/commands/pending")
        assert pending.status_code == 200
        body = pending.json()
        assert body["data"]["count"] == 1
        assert body["data"]["proposals"][0]["command_id"] == command_id

        detail = client.get(f"/ask/commands/{command_id}")
        assert detail.status_code == 200
        assert detail.json()["data"]["tool_name"] == "propose_create_followup"

        missing = client.get(f"/ask/commands/{uuid4()}")
        assert missing.status_code == 404
        assert missing.json()["data"]["error_code"] == "command_not_found"
