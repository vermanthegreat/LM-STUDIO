"""Local LLM planner for /ask when use_llm=True."""

from __future__ import annotations

import json
import logging
from typing import Optional

from llm import call_lmstudio_for_text
from pydantic import ValidationError
from tools.planner import PlannerClarify, PlannerToolCall
from tools.registry import build_default_registry

logger = logging.getLogger(__name__)

_PLANNER_SYSTEM = (
    "You are a local planner for a contacts database. "
    "Return JSON only. Select one registered tool or ask for clarification. "
    "Never invent tools, SQL, or facts."
)
_PLANNER_TIMEOUT_S = 8.0


def _tool_catalog() -> str:
    lines: list[str] = []
    registry = build_default_registry()
    for name in registry.list_tools():
        spec = registry.get(name)
        lines.append(f"- {name}: {spec.description or spec.risk_class.value}")
    return "\n".join(lines)


def _build_planner_prompt(question: str) -> str:
    return f"""Choose one registered tool for this question, or ask for clarification.

Registered tools:
{_tool_catalog()}

Tool output schema:
{{"action":"tool","tool_name":"<registered tool>","arguments":{{}},"reason":"brief reason"}}

Clarify output schema:
{{"action":"clarify","question":"focused clarification question"}}

Question: {question!r}"""


def _parse_planner_payload(raw: str) -> PlannerToolCall | PlannerClarify | None:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    action = str(payload.get("action") or "").strip().lower()
    if action == "clarify":
        try:
            return PlannerClarify.model_validate(payload)
        except ValidationError:
            return None
    if action == "tool":
        try:
            return PlannerToolCall.model_validate(payload)
        except ValidationError:
            return None
    return None


def plan_question_with_local_llm(
    question: str,
    *,
    timeout_s: float = _PLANNER_TIMEOUT_S,
) -> PlannerToolCall | PlannerClarify | None:
    """Ask the local LM Studio planner for a tool call or clarification."""
    q = (question or "").strip()
    if not q:
        return None

    raw = call_lmstudio_for_text(
        _build_planner_prompt(q),
        timeout_s=timeout_s,
        system_prompt=_PLANNER_SYSTEM,
    )
    if not raw:
        logger.info("Local LLM planner unavailable or returned no output")
        return None

    plan = _parse_planner_payload(raw)
    if plan is None:
        logger.info("Local LLM planner returned unparsable output")
    return plan
