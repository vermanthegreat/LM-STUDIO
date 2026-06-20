"""Shared constants for governed write proposal tools."""

from __future__ import annotations

SUPPORTED_WRITE_PROPOSAL_TOOLS: dict[str, str] = {
    "propose_create_followup": "create_followup",
    "propose_contact_update": "update_contact_field",
}

WRITE_PROPOSAL_TOOL_NAMES = frozenset(SUPPORTED_WRITE_PROPOSAL_TOOLS.keys())
