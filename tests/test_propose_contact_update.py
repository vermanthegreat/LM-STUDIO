"""Tests for propose_contact_update governed write proposals."""

from __future__ import annotations

from uuid import UUID, uuid4

import db
import pytest
from fastapi.testclient import TestClient

from app import create_app
from ask_router import (
    answer_question,
    apply_write_proposal_route,
    approve_write_proposal_route,
    execute_planner_propose_tool_route,
)
from config import AppConfig
from repositories.sqlite_store import SqliteContactStore
from services.command_log import CommandStatus, InMemoryCommandLog
from services.command_service import CommandService
from tools.registry import ToolValidationError, build_default_registry


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


def _proposal_payload(lead_id: int, *, value: str = "sales@acme.example") -> dict:
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


def _create_proposal(service: CommandService, store: SqliteContactStore, lead_id: int):
    return execute_planner_propose_tool_route(
        "update Acme email",
        _proposal_payload(lead_id),
        store=store,
        command_service=service,
    )


def test_propose_contact_update_creates_preview(tmp_path):
    db_path = tmp_path / "test.db"
    lead_id = _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=InMemoryCommandLog())

    result = answer_question(
        "update email for Acme",
        use_llm=False,
        store=store,
        command_service=service,
        planner_payload=_proposal_payload(lead_id),
    )

    assert result["intent"] == "write_proposal"
    assert result["data"]["command_status"] == CommandStatus.AWAITING_APPROVAL.value
    assert result["data"]["proposal"]["field"] == "company_email"
    assert result["data"]["proposal"]["value"] == "sales@acme.example"
    assert result["data"]["record_count"] == 0

    lead = store.get_lead(lead_id)
    assert lead is not None
    assert lead["company_email"] == "hello@acme.example"
    assert lead["fit_score"] == 75


def test_unapproved_apply_rejected(tmp_path):
    db_path = tmp_path / "test.db"
    lead_id = _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=InMemoryCommandLog())

    proposal = _create_proposal(service, store, lead_id)
    command_id = UUID(proposal["data"]["command_id"])

    result = apply_write_proposal_route(command_id, store=store, command_service=service)
    assert result["status"] == "error"
    assert "not been approved" in result["answer"]

    lead = store.get_lead(lead_id)
    assert lead is not None
    assert lead["company_email"] == "hello@acme.example"


def test_approved_apply_updates_exactly_one_field(tmp_path):
    db_path = tmp_path / "test.db"
    lead_id = _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=InMemoryCommandLog())

    proposal = _create_proposal(service, store, lead_id)
    command_id = UUID(proposal["data"]["command_id"])

    approve = approve_write_proposal_route(command_id, store=store, command_service=service)
    assert approve["status"] == "ok"

    applied = apply_write_proposal_route(command_id, store=store, command_service=service)
    assert applied["status"] == "ok"
    assert applied["data"]["record_count"] == 1
    assert applied["data"]["records"][0]["company_email"] == "sales@acme.example"

    lead = store.get_lead(lead_id)
    assert lead is not None
    assert lead["company_email"] == "sales@acme.example"
    assert lead["company_phone"] == "+1-555-0100"
    assert lead["website"] == "https://acme.example"
    assert lead["fit_score"] == 75


def test_malformed_and_stale_proposals_rejected(tmp_path):
    db_path = tmp_path / "test.db"
    lead_id = _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=InMemoryCommandLog())

    missing = _create_proposal(service, store, lead_id)
    missing_id = UUID(missing["data"]["command_id"])
    entry = service.get_command(missing_id)
    assert entry is not None
    entry.result_summary = {"reason": "only"}
    service.command_log.update(entry)
    bad_approve = approve_write_proposal_route(missing_id, store=store, command_service=service)
    assert bad_approve["status"] == "error"
    assert "stored write proposal" in bad_approve["answer"].lower()

    stale = _create_proposal(service, store, lead_id)
    stale_id = UUID(stale["data"]["command_id"])
    stale_entry = service.get_command(stale_id)
    assert stale_entry is not None
    stale_entry.approved_at = stale_entry.updated_at
    stale_entry.result_summary = {
        "reason": "x",
        "proposal": {
            "action": "update_contact_field",
            "lead_id": lead_id,
            "field": "company_email",
            "value": "",
        },
    }
    service.command_log.update(stale_entry)
    bad_apply = apply_write_proposal_route(stale_id, store=store, command_service=service)
    assert bad_apply["status"] == "error"
    assert "contact field value" in bad_apply["answer"].lower()

    lead = store.get_lead(lead_id)
    assert lead is not None
    assert lead["company_email"] == "hello@acme.example"


def test_apply_ignores_request_body_and_uses_command_log(tmp_path):
    db_path = tmp_path / "routes.db"
    lead_id = _seed(db_path)
    cfg = AppConfig(database_path=db_path, max_paste_chars=1000, port=8025)
    client = TestClient(create_app(cfg), base_url="http://127.0.0.1:8025")
    store = SqliteContactStore(db_path)
    service = CommandService(store)
    proposal = _create_proposal(service, store, lead_id)
    command_id = proposal["data"]["command_id"]

    with client:
        assert client.post(f"/ask/commands/{command_id}/approve").status_code == 200
        apply = client.post(
            f"/ask/commands/{command_id}/apply",
            json={"proposal": {"field": "company_email", "value": "evil@acme.example"}},
        )
        assert apply.status_code == 200

    lead = store.get_lead(lead_id)
    assert lead is not None
    assert lead["company_email"] == "sales@acme.example"


def test_unknown_tool_fields_rejected_by_registry():
    registry = build_default_registry()
    with pytest.raises(ToolValidationError):
        registry.validate_arguments(
            "propose_contact_update",
            {
                "lead_id": 1,
                "field": "company_email",
                "value": "ok@example.com",
                "sql": "DROP TABLE leads",
            },
        )


def test_missing_lead_rejected_at_preview(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=InMemoryCommandLog())

    result = execute_planner_propose_tool_route(
        "update missing lead",
        _proposal_payload(9999),
        store=store,
        command_service=service,
    )
    assert result["intent"] == "tool_error"
    assert "Lead not found" in result["answer"]


def test_unknown_command_id_rejected(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=InMemoryCommandLog())
    missing = uuid4()

    approve = approve_write_proposal_route(missing, store=store, command_service=service)
    assert approve["data"]["error_code"] == "command_not_found"

    apply = apply_write_proposal_route(missing, store=store, command_service=service)
    assert apply["data"]["error_code"] == "command_not_found"
