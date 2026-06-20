"""Validate planner tool-call output against the typed tool registry."""

from __future__ import annotations

from typing import Any, Dict, Optional, TYPE_CHECKING

from pydantic import ValidationError

from tools.planner import PlannerToolCall
from tools.registry import ToolRegistryError, ToolValidationError, UnknownToolError

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
