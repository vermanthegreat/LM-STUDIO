"""Apply approved write proposals to the contact store."""

from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

import db
from repositories import ContactStore
from services.command_log import CommandLogEntry, CommandStatus, transition
from services.write_proposal_authority import load_stored_write_proposal
from tools.envelope import ToolResult
from tools.write_handlers import WriteProposalError


def apply_write_proposal(store: ContactStore, entry: CommandLogEntry) -> ToolResult:
    existing = find_existing_apply_result(store, entry)
    if existing is not None:
        return existing

    proposal = load_stored_write_proposal(entry)
    action = proposal.get("action")

    if entry.tool_name == "propose_create_followup" or action == "create_followup":
        return _apply_create_followup(store, entry, proposal)
    if entry.tool_name == "propose_contact_update" or action == "update_contact_field":
        return _apply_contact_update(store, entry, proposal)

    raise WriteProposalError(f"Unsupported write proposal action: {action}")


def find_existing_apply_result(
    store: ContactStore,
    entry: CommandLogEntry,
) -> Optional[ToolResult]:
    proposal = load_stored_write_proposal(entry)
    action = proposal.get("action")

    if entry.tool_name == "propose_create_followup" or action == "create_followup":
        return _existing_followup_result(store, entry, proposal)
    if entry.tool_name == "propose_contact_update" or action == "update_contact_field":
        return _existing_contact_update_result(store, entry, proposal)
    return None


def finalize_recovered_apply(
    store: ContactStore,
    command_log: Any,
    entry: CommandLogEntry,
) -> Optional[ToolResult]:
    result = find_existing_apply_result(store, entry)
    if result is None:
        return None

    transition(entry, CommandStatus.SUCCEEDED)
    summary = dict(entry.result_summary or {})
    summary["applied_result"] = result.model_dump(mode="json")
    entry.result_summary = summary

    def _persist() -> ToolResult:
        command_log.update(entry)
        result.command_id = entry.id
        return result

    if hasattr(store, "transaction"):
        with store.transaction():
            return _persist()
    return _persist()


def _store_db_kwargs(store: ContactStore) -> dict[str, Any]:
    if hasattr(store, "_kwargs"):
        return store._kwargs()
    database_path = getattr(store, "database_path", None)
    if database_path is not None:
        return {"db_path": database_path}
    return {}


def _postgres_find_task_by_command(store: ContactStore, command_id: UUID) -> Optional[dict[str, Any]]:
    from persistence.models import Task
    from persistence.session import session_scope
    from sqlalchemy import select

    database_url = getattr(store, "database_url", None)
    if not database_url:
        return None
    with session_scope(database_url) as session:
        row = session.scalar(select(Task).where(Task.created_by_command_id == command_id))
        if row is None:
            return None
        return {
            "id": row.legacy_task_id,
            "title": row.title,
            "status": row.status,
        }


def _existing_followup_result(
    store: ContactStore,
    entry: CommandLogEntry,
    proposal: dict[str, Any],
) -> Optional[ToolResult]:
    backend = getattr(store, "backend", None)
    task: Optional[dict[str, Any]] = None
    if backend == "sqlite":
        task = db.find_task_by_created_by_command_id(str(entry.id), **_store_db_kwargs(store))
    elif backend == "postgresql":
        task = _postgres_find_task_by_command(store, entry.id)
    if task is None:
        return None
    lead_id = proposal["lead_id"]
    title = proposal["title"]
    lead = store.get_lead(lead_id)
    company_name = lead.get("company_name") if lead else proposal.get("company_name")
    return ToolResult(
        tool_name=entry.tool_name or "propose_create_followup",
        status="ok",
        summary=f'Follow-up task "{title}" already applied for {company_name}.',
        records=[task],
        record_count=1,
        provenance=["repository:find_task_by_created_by_command_id", "command_log:proposal"],
        command_id=entry.id,
    )


def _existing_contact_update_result(
    store: ContactStore,
    entry: CommandLogEntry,
    proposal: dict[str, Any],
) -> Optional[ToolResult]:
    lead_id = proposal["lead_id"]
    field = proposal["field"]
    value = (proposal.get("value") or "").strip()
    lead = store.get_lead(lead_id)
    if lead is None:
        return None
    current = (lead.get(field) or "").strip()
    if current != value:
        return None
    return ToolResult(
        tool_name=entry.tool_name or "propose_contact_update",
        status="ok",
        summary=(
            f'{field} for {lead.get("company_name")} is already "{current}".'
        ),
        records=[lead],
        record_count=1,
        provenance=["repository:get_lead", "command_log:proposal"],
        command_id=entry.id,
    )


def _apply_create_followup(
    store: ContactStore,
    entry: CommandLogEntry,
    proposal: dict[str, Any],
) -> ToolResult:
    lead_id = proposal["lead_id"]
    title = proposal["title"]

    lead = store.get_lead(lead_id)
    if lead is None:
        raise WriteProposalError(f"Lead not found: {lead_id}")

    task_data = {
        "title": title,
        "due_date": proposal.get("due_date"),
        "priority": proposal.get("priority"),
        "status": "open",
        "created_by_command_id": str(entry.id),
    }
    if proposal.get("idempotency_key"):
        task_data["idempotency_key"] = proposal["idempotency_key"]
    task = store.add_task(lead_id, task_data)
    return ToolResult(
        tool_name=entry.tool_name or "propose_create_followup",
        status="ok",
        summary=f'Created follow-up task "{title}" for {lead.get("company_name")}.',
        records=[task],
        record_count=1,
        provenance=["repository:add_task", "command_log:proposal"],
        command_id=entry.id,
    )


def _apply_contact_update(
    store: ContactStore,
    entry: CommandLogEntry,
    proposal: dict[str, Any],
) -> ToolResult:
    lead_id = proposal["lead_id"]
    field = proposal["field"]
    value = proposal["value"]

    existing = _existing_contact_update_result(store, entry, proposal)
    if existing is not None:
        return existing

    try:
        updated = store.update_lead_contact_field(lead_id, field, value)
    except ValueError as exc:
        raise WriteProposalError(str(exc)) from exc

    return ToolResult(
        tool_name=entry.tool_name or "propose_contact_update",
        status="ok",
        summary=(
            f'Updated {field} for {updated.get("company_name")} to "{updated.get(field)}".'
        ),
        records=[updated],
        record_count=1,
        provenance=["repository:update_lead_contact_field", "command_log:proposal"],
        command_id=entry.id,
    )
