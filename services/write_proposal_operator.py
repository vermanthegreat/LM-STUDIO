"""Operator-facing read models for pending write proposals."""

from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from services.command_log import CommandLogEntry, CommandStatus
from services.command_service import CommandService
from services.write_proposal_constants import WRITE_PROPOSAL_TOOL_NAMES


def _proposal_summary(entry: CommandLogEntry) -> Optional[str]:
    if not entry.result_summary:
        return None
    preview = entry.result_summary.get("proposal_preview")
    if isinstance(preview, dict):
        summary = preview.get("summary")
        if isinstance(summary, str) and summary:
            return summary
    return None


def _is_pending_write_proposal(entry: CommandLogEntry) -> bool:
    if entry.status != CommandStatus.AWAITING_APPROVAL:
        return False
    if not entry.requires_approval:
        return False
    if entry.tool_name not in WRITE_PROPOSAL_TOOL_NAMES:
        return False
    proposal = (entry.result_summary or {}).get("proposal")
    return isinstance(proposal, dict)


def format_write_proposal_list_item(entry: CommandLogEntry) -> dict[str, Any]:
    proposal = (entry.result_summary or {}).get("proposal", {})
    return {
        "command_id": str(entry.id),
        "status": entry.status.value,
        "tool_name": entry.tool_name,
        "summary": _proposal_summary(entry),
        "proposal": proposal,
        "created_at": entry.created_at.isoformat(),
        "approved_at": entry.approved_at.isoformat() if entry.approved_at else None,
    }


def format_write_proposal_detail(entry: CommandLogEntry) -> dict[str, Any]:
    proposal = (entry.result_summary or {}).get("proposal", {})
    return {
        "command_id": str(entry.id),
        "status": entry.status.value,
        "tool_name": entry.tool_name,
        "summary": _proposal_summary(entry),
        "proposal": proposal,
        "command_text": entry.command_text,
        "created_at": entry.created_at.isoformat(),
        "approved_at": entry.approved_at.isoformat() if entry.approved_at else None,
        "requires_approval": entry.requires_approval,
    }


def list_pending_write_proposals(service: CommandService) -> list[CommandLogEntry]:
    entries = service.command_log.list_pending_write_proposals()
    return [entry for entry in entries if _is_pending_write_proposal(entry)]
