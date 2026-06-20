"""Validate persisted write proposals before approval or apply."""

from __future__ import annotations

from typing import Any

from services.command_log import CommandLogEntry
from services.write_proposal_constants import SUPPORTED_WRITE_PROPOSAL_TOOLS
from tools.write_handlers import WriteProposalError

_SUPPORTED_WRITE_PROPOSAL_TOOLS = SUPPORTED_WRITE_PROPOSAL_TOOLS

_ALLOWED_CONTACT_FIELDS = frozenset({"company_email", "company_phone", "website"})


def load_stored_write_proposal(entry: CommandLogEntry) -> dict[str, Any]:
    """Return the authoritative proposal stored on command_log."""
    expected_action = _SUPPORTED_WRITE_PROPOSAL_TOOLS.get(entry.tool_name or "")
    if not expected_action:
        raise WriteProposalError(f"Unsupported write proposal tool: {entry.tool_name}")

    if not entry.result_summary:
        raise WriteProposalError("Command has no stored write proposal")

    proposal = entry.result_summary.get("proposal")
    if not isinstance(proposal, dict):
        raise WriteProposalError("Stored write proposal is missing or malformed")

    action = proposal.get("action")
    if action not in (None, expected_action):
        raise WriteProposalError("Stored write proposal action is invalid")

    if entry.tool_name == "propose_create_followup":
        _validate_followup_proposal(proposal)
    elif entry.tool_name == "propose_contact_update":
        _validate_contact_update_proposal(proposal)

    return proposal


def _validate_followup_proposal(proposal: dict[str, Any]) -> None:
    lead_id = proposal.get("lead_id")
    title = proposal.get("title")
    if not isinstance(lead_id, int) or not isinstance(title, str) or not title.strip():
        raise WriteProposalError("Stored write proposal is missing lead_id or title")


def _validate_contact_update_proposal(proposal: dict[str, Any]) -> None:
    lead_id = proposal.get("lead_id")
    field = proposal.get("field")
    value = proposal.get("value")
    if not isinstance(lead_id, int):
        raise WriteProposalError("Stored write proposal is missing lead_id")
    if field not in _ALLOWED_CONTACT_FIELDS:
        raise WriteProposalError("Stored write proposal has invalid contact field")
    if not isinstance(value, str) or not value.strip():
        raise WriteProposalError("Stored write proposal is missing contact field value")
