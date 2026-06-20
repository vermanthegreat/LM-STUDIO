"""Apply approved write proposals to the contact store."""

from __future__ import annotations

from typing import Any

from repositories import ContactStore
from services.command_log import CommandLogEntry
from tools.envelope import ToolResult
from tools.write_handlers import WriteProposalError


def apply_write_proposal(store: ContactStore, entry: CommandLogEntry) -> ToolResult:
    if not entry.result_summary or "proposal" not in entry.result_summary:
        raise WriteProposalError("Command has no stored write proposal")

    proposal: dict[str, Any] = entry.result_summary["proposal"]
    action = proposal.get("action")

    if entry.tool_name == "propose_create_followup" or action == "create_followup":
        return _apply_create_followup(store, entry, proposal)

    raise WriteProposalError(f"Unsupported write proposal action: {action}")


def _apply_create_followup(
    store: ContactStore,
    entry: CommandLogEntry,
    proposal: dict[str, Any],
) -> ToolResult:
    lead_id = proposal.get("lead_id")
    title = proposal.get("title")
    if not isinstance(lead_id, int) or not title:
        raise WriteProposalError("Proposal is missing lead_id or title")

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
        provenance=["repository:add_task"],
        command_id=entry.id,
    )
