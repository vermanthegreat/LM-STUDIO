"""Standard tool result envelope per docs/tool-contracts.md."""

from __future__ import annotations

from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str
    status: Literal["ok", "error"]
    summary: str
    records: list[dict[str, Any]] = Field(default_factory=list)
    proposal: Optional[dict[str, Any]] = None
    record_count: int = 0
    warnings: list[str] = Field(default_factory=list)
    provenance: list[str] = Field(default_factory=list)
    command_id: Optional[UUID] = None
