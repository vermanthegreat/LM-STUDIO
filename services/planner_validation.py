"""Validate planner tool-call output against the typed tool registry."""

from __future__ import annotations

from typing import Any, Dict, Optional, TYPE_CHECKING
from uuid import UUID

from pydantic import ValidationError

from services.command_log import CommandLogError
from tools.planner import PlannerToolCall
from tools.registry import ToolRegistryError, ToolValidationError, UnknownToolError
from tools.write_handlers import WriteProposalError
from tools.risk import RiskClass

if TYPE_CHECKING:
    from services.command_service import CommandService


def validate_planner_tool_call(
    service: CommandService,
    *,
    command_text: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Validate a proposed planner tool call and record the command-log outcome."""
    entry = service.receive(command_text)
    try:
        plan = PlannerToolCall.model_validate(payload)
    except ValidationError as exc:
        service.reject(entry, code="planner_validation_error", message=str(exc))
        return _planner_error(
            intent="planner_validation_error",
            error_code="planner_validation_error",
            message=str(exc),
            entry_id=entry.id,
            command_status=entry.status.value,
        )

    try:
        service.registry.get(plan.tool_name)
        validated = service.registry.validate_arguments(plan.tool_name, plan.arguments)
        plan = PlannerToolCall(
            tool_name=plan.tool_name,
            arguments=validated.model_dump(mode="json"),
            reason=plan.reason,
        )
    except UnknownToolError as exc:
        service.reject(entry, code="UnknownToolError", message=str(exc))
        return _planner_error(
            intent="tool_error",
            error_code="UnknownToolError",
            message=str(exc),
            entry_id=entry.id,
            command_status=entry.status.value,
            tool_name=plan.tool_name,
        )
    except ToolValidationError as exc:
        service.reject(entry, code="ToolValidationError", message=str(exc))
        return _planner_error(
            intent="tool_error",
            error_code="ToolValidationError",
            message=str(exc),
            entry_id=entry.id,
            command_status=entry.status.value,
            tool_name=plan.tool_name,
        )
    except ToolRegistryError as exc:
        service.reject(entry, code=type(exc).__name__, message=str(exc))
        return _planner_error(
            intent="tool_error",
            error_code=type(exc).__name__,
            message=str(exc),
            entry_id=entry.id,
            command_status=entry.status.value,
            tool_name=plan.tool_name,
        )

    service.plan_tool_call(entry, plan)
    return {
        "status": "ok",
        "intent": "planned",
        "plan": plan,
        "data": {
            "command_id": str(entry.id),
            "command_status": entry.status.value,
            "tool_name": plan.tool_name,
            "tool_arguments": plan.arguments,
        },
    }


def _planner_error(
    *,
    intent: str,
    error_code: str,
    message: str,
    entry_id,
    command_status: str,
    tool_name: Optional[str] = None,
) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "command_id": str(entry_id),
        "command_status": command_status,
        "error_code": error_code,
    }
    if tool_name is not None:
        data["tool_name"] = tool_name
    return {
        "status": "error",
        "intent": intent,
        "error_code": error_code,
        "message": message,
        "data": data,
    }


def execute_planner_read_tool_call(
    service: CommandService,
    *,
    command_text: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Validate one planner tool call and execute it only when read-only."""
    validation = validate_planner_tool_call(
        service,
        command_text=command_text,
        payload=payload,
    )
    if validation["status"] != "ok":
        return validation

    entry = service.get_command(UUID(validation["data"]["command_id"]))
    if entry is None or entry.tool_name is None:
        return _planner_error(
            intent="tool_error",
            error_code="CommandLogError",
            message="Validated planner command is missing from command log.",
            entry_id=UUID(validation["data"]["command_id"]),
            command_status="failed",
        )

    spec = service.registry.get(entry.tool_name)
    if spec.risk_class != RiskClass.READ:
        service.reject(
            entry,
            code="non_read_tool_rejected",
            message=f"Tool {entry.tool_name} is not read-only.",
        )
        return _planner_error(
            intent="tool_error",
            error_code="non_read_tool_rejected",
            message=f"Tool {entry.tool_name} is not read-only.",
            entry_id=entry.id,
            command_status=entry.status.value,
            tool_name=entry.tool_name,
        )

    try:
        result = service.execute_planned_tool(entry)
    except (CommandLogError, ToolRegistryError) as exc:
        updated = service.get_command(entry.id)
        return _planner_error(
            intent="tool_error",
            error_code=type(exc).__name__,
            message=str(exc),
            entry_id=entry.id,
            command_status=updated.status.value if updated else "failed",
            tool_name=entry.tool_name,
        )

    return {
        "status": "ok",
        "tool_name": entry.tool_name,
        "result": result,
        "entry": entry,
        "plan": validation["plan"],
        "data": {
            **validation["data"],
            "command_status": entry.status.value,
        },
    }


def execute_planner_propose_tool_call(
    service: CommandService,
    *,
    command_text: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Validate one planner tool call and preview it only when propose-class."""
    validation = validate_planner_tool_call(
        service,
        command_text=command_text,
        payload=payload,
    )
    if validation["status"] != "ok":
        return validation

    entry = service.get_command(UUID(validation["data"]["command_id"]))
    if entry is None or entry.tool_name is None:
        return _planner_error(
            intent="tool_error",
            error_code="CommandLogError",
            message="Validated planner command is missing from command log.",
            entry_id=UUID(validation["data"]["command_id"]),
            command_status="failed",
        )

    spec = service.registry.get(entry.tool_name)
    if not spec.risk_class.creates_write_proposal:
        service.reject(
            entry,
            code="non_propose_tool_rejected",
            message=f"Tool {entry.tool_name} is not a write-proposal tool.",
        )
        return _planner_error(
            intent="tool_error",
            error_code="non_propose_tool_rejected",
            message=f"Tool {entry.tool_name} is not a write-proposal tool.",
            entry_id=entry.id,
            command_status=entry.status.value,
            tool_name=entry.tool_name,
        )

    try:
        result = service.execute_planned_tool(entry)
    except (CommandLogError, ToolRegistryError, WriteProposalError) as exc:
        updated = service.get_command(entry.id)
        return _planner_error(
            intent="tool_error",
            error_code=type(exc).__name__,
            message=str(exc),
            entry_id=entry.id,
            command_status=updated.status.value if updated else "failed",
            tool_name=entry.tool_name,
        )

    updated = service.get_command(entry.id)
    if updated is not None:
        entry = updated

    return {
        "status": "ok",
        "tool_name": entry.tool_name,
        "result": result,
        "entry": entry,
        "plan": validation["plan"],
        "data": {
            **validation["data"],
            "command_status": entry.status.value,
            "requires_approval": entry.requires_approval,
            "proposal": result.proposal,
        },
    }
