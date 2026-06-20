"""Phase 2 command orchestration — typed tools only, no model SQL."""

from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from repositories import ContactStore
from services.command_log import (
    CommandLogEntry,
    CommandLogError,
    CommandStatus,
    InMemoryCommandLog,
    transition,
)
from tools.envelope import ToolResult
from tools.planner import PlannerClarify, PlannerToolCall
from tools.registry import ToolRegistry, ToolRegistryError, UnknownToolError, build_default_registry
from tools.risk import RiskClass


class CommandService:
    def __init__(
        self,
        store: ContactStore,
        registry: Optional[ToolRegistry] = None,
        command_log: Optional[InMemoryCommandLog] = None,
    ) -> None:
        self.store = store
        self.registry = registry or build_default_registry()
        self.command_log = command_log or InMemoryCommandLog()

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
        if spec.risk_class.requires_approval:
            transition(entry, CommandStatus.AWAITING_APPROVAL)
            entry.requires_approval = True
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
        except ToolRegistryError as exc:
            transition(entry, CommandStatus.FAILED)
            entry.error_code = type(exc).__name__
            entry.error_message = str(exc)
            self.command_log.update(entry)
            raise

        transition(entry, CommandStatus.SUCCEEDED)
        entry.result_summary = result.model_dump(mode="json")
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
