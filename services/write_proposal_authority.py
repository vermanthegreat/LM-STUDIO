"""Validate persisted write proposals before approval or apply."""

from __future__ import annotations

from typing import Any

from services.command_log import CommandLogEntry
from tools.write_handlers import WriteProposalError


def load_stored_write_proposal(entry: CommandLogEntry) -> dict[str, Any]:
    """Return the authoritative proposal stored on command_log."""
    if entry.tool_name != "propose_create_followup":
        raise WriteProposalError(f"Unsupported write proposal tool: {entry.tool_name}")

    if not entry.result_summary:
        raise WriteProposalError("Command has no stored write proposal")

    proposal = entry.result_summary.get("proposal")
    if not isinstance(proposal, dict):
        raise WriteProposalError("Stored write proposal is missing or malformed")

    action = proposal.get("action")
    if action not in (None, "create_followup"):
        raise WriteProposalError("Stored write proposal action is invalid")

    lead_id = proposal.get("lead_id")
    title = proposal.get("title")
    if not isinstance(lead_id, int) or not isinstance(title, str) or not title.strip():
        raise WriteProposalError("Stored write proposal is missing lead_id or title")

    return proposal
