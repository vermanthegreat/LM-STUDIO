"""Tests for command_log state machine."""

from __future__ import annotations

import pytest
from services.command_log import (
    CommandLogError,
    CommandStatus,
    InMemoryCommandLog,
    transition,
)


def test_valid_command_lifecycle():
    log = InMemoryCommandLog()
    entry = log.create("count leads")
    transition(entry, CommandStatus.PLANNED)
    transition(entry, CommandStatus.EXECUTING)
    transition(entry, CommandStatus.SUCCEEDED)
    assert entry.status == CommandStatus.SUCCEEDED
    assert entry.status.is_terminal


def test_approval_path():
    log = InMemoryCommandLog()
    entry = log.create("bulk task create")
    transition(entry, CommandStatus.PLANNED)
    transition(entry, CommandStatus.AWAITING_APPROVAL)
    transition(entry, CommandStatus.EXECUTING)
    transition(entry, CommandStatus.SUCCEEDED)


def test_invalid_transition_rejected():
    log = InMemoryCommandLog()
    entry = log.create("bad path")
    with pytest.raises(CommandLogError):
        transition(entry, CommandStatus.SUCCEEDED)
