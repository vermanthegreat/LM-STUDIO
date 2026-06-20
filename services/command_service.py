"""Phase 2 command orchestration — typed tools only, no model SQL."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from repositories import ContactStore
from repositories.command_log_store import CommandLogStore, get_command_log_store
from services.command_log import (
    CommandLogEntry,
    CommandLogError,
    CommandStatus,
    transition,
)
from services.write_proposal_authority import load_stored_write_proposal
from services.write_proposals import apply_write_proposal
from tools.envelope import ToolResult
from tools.planner import PlannerClarify, PlannerToolCall
from tools.registry import ToolRegistry, ToolRegistryError, UnknownToolError, build_default_registry
from tools.risk import RiskClass
from tools.write_handlers import WriteProposalError


class CommandService:
    def __init__(
        self,
        store: ContactStore,
        registry: Optional[ToolRegistry] = None,
        command_log: Optional[CommandLogStore] = None,
    ) -> None:
        self.store = store
        self.registry = registry or build_default_registry()
        self.command_log = command_log if command_log is not None else get_command_log_store(store)

    def receive(self, command_text: str, *, correlation_id: Optional[str] = None) -> CommandLogEntry:
        return self.command_log.create(command_text, correlation_id=correlation_id)

    def plan_tool_call(
        self,
        entry: CommandLogEntry,
        plan: PlannerToolCall,
    ) -> CommandLogEntry:
        transition(entry, CommandStatus.PLANNED)
        entry.intent = "tool"
        entry.tool_name = plan.tool_name
        entry.tool_arguments = dict(plan.arguments)
        entry.result_summary = {"reason": plan.reason}
        self.command_log.update(entry)
        return entry

    def execute_planned_tool(self, entry: CommandLogEntry) -> ToolResult:
        if not entry.tool_name:
            raise CommandLogError("Command has no planned tool")
        spec = self.registry.get(entry.tool_name)
        if spec.risk_class.requires_approval and not spec.risk_class.creates_write_proposal:
            transition(entry, CommandStatus.AWAITING_APPROVAL)
            entry.requires_approval = True
            entry.risk_class = spec.risk_class.value
            self.command_log.update(entry)
            raise CommandLogError("Tool requires approval before execution")

        transition(entry, CommandStatus.EXECUTING)
        entry.risk_class = spec.risk_class.value
        self.command_log.update(entry)
        try:
            result = self.registry.execute(
                self.store,
                entry.tool_name,
                entry.tool_arguments or {},
            )
        except (ToolRegistryError, WriteProposalError) as exc:
            transition(entry, CommandStatus.FAILED)
            entry.error_code = type(exc).__name__
            entry.error_message = str(exc)
            self.command_log.update(entry)
            raise

        if spec.risk_class.creates_write_proposal:
            return self._store_write_proposal(entry, result)

        transition(entry, CommandStatus.SUCCEEDED)
        entry.result_summary = result.model_dump(mode="json")
        self.command_log.update(entry)
        result.command_id = entry.id
        return result

    def approve_command(self, entry: CommandLogEntry) -> CommandLogEntry:
        entry = self._reload_command_entry(entry.id)
        if entry.status == CommandStatus.SUCCEEDED:
            raise CommandLogError("Write proposal has already been applied")
        if entry.status == CommandStatus.FAILED:
            raise CommandLogError("Command is in failed state and cannot be approved")
        if entry.status == CommandStatus.REJECTED:
            raise CommandLogError("Command was rejected and cannot be approved")
        if entry.status != CommandStatus.AWAITING_APPROVAL:
            raise CommandLogError("Command is not awaiting approval")
        if not entry.requires_approval:
            raise CommandLogError("Command does not require approval")
        try:
            load_stored_write_proposal(entry)
        except WriteProposalError as exc:
            raise CommandLogError(str(exc)) from exc
        entry.approved_at = datetime.now(timezone.utc)
        self.command_log.update(entry)
        return entry

    def apply_approved_command(self, entry: CommandLogEntry) -> ToolResult:
        entry = self._reload_command_entry(entry.id)
        if entry.status == CommandStatus.SUCCEEDED:
            raise CommandLogError("Write proposal has already been applied")
        if entry.status == CommandStatus.FAILED:
            raise CommandLogError("Command is in failed state and cannot be applied")
        if entry.status == CommandStatus.REJECTED:
            raise CommandLogError("Command was rejected and cannot be applied")
        if entry.status != CommandStatus.AWAITING_APPROVAL:
            raise CommandLogError("Command is not awaiting approval")
        if not entry.requires_approval:
            raise CommandLogError("Command does not require approval")
        if entry.approved_at is None:
            raise CommandLogError("Command has not been approved")

        transition(entry, CommandStatus.EXECUTING)
        self.command_log.update(entry)
        try:
            result = apply_write_proposal(self.store, entry)
        except WriteProposalError as exc:
            transition(entry, CommandStatus.FAILED)
            entry.error_code = type(exc).__name__
            entry.error_message = str(exc)
            self.command_log.update(entry)
            raise CommandLogError(str(exc)) from exc

        transition(entry, CommandStatus.SUCCEEDED)
        summary = dict(entry.result_summary or {})
        summary["applied_result"] = result.model_dump(mode="json")
        entry.result_summary = summary
        self.command_log.update(entry)
        result.command_id = entry.id
        return result

    def _reload_command_entry(self, command_id: UUID) -> CommandLogEntry:
        entry = self.command_log.get(command_id)
        if entry is None:
            raise CommandLogError(f"Command {command_id} was not found")
        return entry

    def _store_write_proposal(self, entry: CommandLogEntry, result: ToolResult) -> ToolResult:
        transition(entry, CommandStatus.AWAITING_APPROVAL)
        entry.requires_approval = True
        prior = dict(entry.result_summary or {})
        entry.result_summary = {
            **prior,
            "proposal": result.proposal,
            "proposal_preview": result.model_dump(mode="json"),
        }
        self.command_log.update(entry)
        result.command_id = entry.id
        return result

    def reject(self, entry: CommandLogEntry, *, code: str, message: str) -> CommandLogEntry:
        transition(entry, CommandStatus.REJECTED)
        entry.error_code = code
        entry.error_message = message
        self.command_log.update(entry)
        return entry

    def clarify(self, entry: CommandLogEntry, plan: PlannerClarify) -> dict[str, Any]:
        transition(entry, CommandStatus.PLANNED)
        entry.intent = "clarify"
        entry.result_summary = {"question": plan.question}
        self.command_log.update(entry)
        return {"action": "clarify", "question": plan.question, "command_id": str(entry.id)}

    def get_command(self, command_id: UUID) -> Optional[CommandLogEntry]:
        return self.command_log.get(command_id)
