"""Command log state machine for Phase 2 planning."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import UUID, uuid4

from services.write_proposal_constants import WRITE_PROPOSAL_TOOL_NAMES


class CommandStatus(str, Enum):
    RECEIVED = "received"
    PLANNED = "planned"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in {
            CommandStatus.SUCCEEDED,
            CommandStatus.REJECTED,
            CommandStatus.FAILED,
        }


_ALLOWED_TRANSITIONS: dict[CommandStatus, set[CommandStatus]] = {
    CommandStatus.RECEIVED: {
        CommandStatus.PLANNED,
        CommandStatus.REJECTED,
        CommandStatus.FAILED,
    },
    CommandStatus.PLANNED: {
        CommandStatus.AWAITING_APPROVAL,
        CommandStatus.EXECUTING,
        CommandStatus.REJECTED,
        CommandStatus.FAILED,
    },
    CommandStatus.AWAITING_APPROVAL: {
        CommandStatus.EXECUTING,
        CommandStatus.REJECTED,
        CommandStatus.FAILED,
    },
    CommandStatus.EXECUTING: {
        CommandStatus.SUCCEEDED,
        CommandStatus.AWAITING_APPROVAL,
        CommandStatus.FAILED,
    },
    CommandStatus.SUCCEEDED: set(),
    CommandStatus.REJECTED: set(),
    CommandStatus.FAILED: set(),
}


class CommandLogError(Exception):
    """Invalid command-log transition or mutation."""


@dataclass
class CommandLogEntry:
    id: UUID
    command_text: str
    status: CommandStatus = CommandStatus.RECEIVED
    intent: Optional[str] = None
    tool_name: Optional[str] = None
    tool_arguments: Optional[dict[str, Any]] = None
    risk_class: Optional[str] = None
    requires_approval: bool = False
    approved_at: Optional[datetime] = None
    result_summary: Optional[dict[str, Any]] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    correlation_id: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def transition(entry: CommandLogEntry, new_status: CommandStatus) -> None:
    allowed = _ALLOWED_TRANSITIONS.get(entry.status, set())
    if new_status not in allowed:
        raise CommandLogError(
            f"Cannot transition command {entry.id} from {entry.status.value} to {new_status.value}"
        )
    entry.status = new_status
    entry.updated_at = datetime.now(timezone.utc)


class InMemoryCommandLog:
    """In-memory command log for tests or stores without DB configuration."""

    def __init__(self) -> None:
        self._entries: dict[UUID, CommandLogEntry] = {}

    def create(self, command_text: str, *, correlation_id: Optional[str] = None) -> CommandLogEntry:
        entry = CommandLogEntry(
            id=uuid4(),
            command_text=command_text,
            correlation_id=correlation_id,
        )
        self._entries[entry.id] = entry
        return entry

    def get(self, command_id: UUID) -> Optional[CommandLogEntry]:
        return self._entries.get(command_id)

    def update(self, entry: CommandLogEntry) -> None:
        entry.updated_at = datetime.now(timezone.utc)
        self._entries[entry.id] = entry

    def list_pending_write_proposals(self) -> list[CommandLogEntry]:
        tool_names = tuple(sorted(WRITE_PROPOSAL_TOOL_NAMES))
        entries = [
            entry
            for entry in self._entries.values()
            if entry.status == CommandStatus.AWAITING_APPROVAL
            and entry.requires_approval
            and entry.tool_name in tool_names
        ]
        return sorted(entries, key=lambda item: item.created_at, reverse=True)
