"""Apply approved write proposals to the contact store."""

from __future__ import annotations

from typing import Any

from repositories import ContactStore
from services.command_log import CommandLogEntry
from services.write_proposal_authority import load_stored_write_proposal
from tools.envelope import ToolResult
from tools.write_handlers import WriteProposalError


def apply_write_proposal(store: ContactStore, entry: CommandLogEntry) -> ToolResult:
    proposal = load_stored_write_proposal(entry)
    action = proposal.get("action")

    if entry.tool_name == "propose_create_followup" or action == "create_followup":
        return _apply_create_followup(store, entry, proposal)
    if entry.tool_name == "propose_contact_update" or action == "update_contact_field":
        return _apply_contact_update(store, entry, proposal)

    raise WriteProposalError(f"Unsupported write proposal action: {action}")


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
    }
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
