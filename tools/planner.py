"""Planner output models — model may only select registered tools."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class PlannerToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["tool"] = "tool"
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


class PlannerClarify(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["clarify"] = "clarify"
    question: str


PlannerPlan = PlannerToolCall | PlannerClarify
