"""Local LLM planner for /ask when use_llm=True."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from pydantic import BaseModel, ValidationError

import llm
from config import AppConfig
from tools.planner import PlannerClarify, PlannerToolCall
from tools.registry import ToolRegistry, build_default_registry

logger = logging.getLogger(__name__)

_PLANNER_SYSTEM = (
    "You are a local planner for a contacts database. "
    "Return JSON only. Select one registered tool or ask for clarification. "
    "Never invent tools, SQL, or facts."
)

PlannerPlan = PlannerToolCall | PlannerClarify


@dataclass(frozen=True)
class PlannerOutcome:
    """Planner result with failure attribution for /ask responses and audits."""

    plan: Optional[PlannerPlan]
    error_code: Optional[str]
    elapsed_ms: int
    timeout_s: float
    model: str
    reasoning_effort: Optional[str]
    finish_reason: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.plan is not None

    def metadata(self) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "status": "ok" if self.ok else "failed",
            "stage": "planning",
            "model": self.model,
            "elapsed_ms": self.elapsed_ms,
            "timeout_s": self.timeout_s,
            "reasoning_effort": self.reasoning_effort,
            "finish_reason": self.finish_reason,
        }
        if self.plan is not None:
            meta["action"] = self.plan.action
        if self.error_code:
            meta["error_code"] = self.error_code
        return meta


def _schema_type(prop: dict[str, Any], defs: dict[str, Any]) -> str:
    if "$ref" in prop:
        prop = defs.get(prop["$ref"].rsplit("/", 1)[-1], {})
    if "enum" in prop:
        return "one of " + "|".join(str(v) for v in prop["enum"])
    if "anyOf" in prop:
        kinds = [_schema_type(p, defs) for p in prop["anyOf"] if p.get("type") != "null"]
        return " or ".join(kinds) or "null"
    kind = str(prop.get("type", "any"))
    if prop.get("format"):
        kind += f" ({prop['format']})"
    if kind == "array" and isinstance(prop.get("items"), dict):
        kind = f"array of {_schema_type(prop['items'], defs)}"
    return kind


def _compact_arguments(model: type[BaseModel]) -> str:
    """Render a tool input model as a one-line argument signature for the prompt."""
    schema = model.model_json_schema()
    defs = schema.get("$defs", {})
    required = set(schema.get("required", []))
    parts: list[str] = []
    for name, prop in (schema.get("properties") or {}).items():
        text = f"{name}{'' if name in required else '?'}: {_schema_type(prop, defs)}"
        bounds = [
            f"{label}{prop[key]}"
            for key, label in (("minimum", ">="), ("maximum", "<="), ("maxLength", "len<="))
            if key in prop
        ]
        if bounds:
            text += f" [{', '.join(bounds)}]"
        if "default" in prop and prop["default"] is not None:
            text += f" = {json.dumps(prop['default'])}"
        parts.append(text)
    return "{" + "; ".join(parts) + "}" if parts else "{}"


def _tool_catalog(registry: Optional[ToolRegistry] = None) -> str:
    registry = registry or build_default_registry()
    lines: list[str] = []
    for name in registry.list_tools():
        spec = registry.get(name)
        description = spec.description or spec.risk_class.value
        lines.append(
            f"- {name} [{spec.risk_class.value}]: {description}\n"
            f"  arguments: {_compact_arguments(spec.input_model)}"
        )
    return "\n".join(lines)


def _build_planner_prompt(question: str) -> str:
    return f"""Choose one registered tool for this question, or ask for clarification.
Use only the listed argument names; omit optional arguments you do not need.

Registered tools:
{_tool_catalog()}

Tool output schema:
{{"action":"tool","tool_name":"<registered tool>","arguments":{{}},"reason":"brief reason"}}

Clarify output schema:
{{"action":"clarify","question":"focused clarification question"}}

Question: {question!r}"""


def _parse_planner_payload(raw: str) -> PlannerPlan | None:
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


def run_local_planner(question: str, *, config: Optional[AppConfig] = None) -> PlannerOutcome:
    """Ask the local LM Studio planner for a plan, attributing any failure.

    Argument validation against the registry happens later, independently of
    the model (``services.planner_validation``).
    """
    cfg = config or AppConfig.from_env()
    model = llm.LM_MODEL

    def _outcome(plan: Optional[PlannerPlan], error_code: Optional[str], **kwargs: Any) -> PlannerOutcome:
        return PlannerOutcome(
            plan=plan,
            error_code=error_code,
            timeout_s=cfg.planner_timeout,
            model=model,
            reasoning_effort=cfg.planner_reasoning_effort,
            **kwargs,
        )

    q = (question or "").strip()
    if not q:
        return _outcome(None, "planner_empty_question", elapsed_ms=0)

    call = llm.call_lmstudio_planner(
        _build_planner_prompt(q),
        system_prompt=_PLANNER_SYSTEM,
        timeout_s=cfg.planner_timeout,
        max_tokens=cfg.planner_max_tokens,
        reasoning_effort=cfg.planner_reasoning_effort,
        model=model,
    )
    timing = {"elapsed_ms": call.elapsed_ms, "finish_reason": call.finish_reason}
    if call.status != "ok" or call.content is None:
        logger.info("Local LLM planner failed: %s (%d ms)", call.status, call.elapsed_ms)
        return _outcome(None, call.status, **timing)

    plan = _parse_planner_payload(call.content)
    if plan is None:
        logger.info("Local LLM planner returned an invalid plan object")
        return _outcome(None, "planner_invalid_plan", **timing)
    return _outcome(plan, None, **timing)


def plan_question_with_local_llm(question: str) -> PlannerPlan | None:
    """Compatibility wrapper: the plan, or None when planning failed."""
    return run_local_planner(question).plan
