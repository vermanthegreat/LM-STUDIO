"""Standard tool result envelope per docs/tool-contracts.md."""

from __future__ import annotations

from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str
    status: Literal["ok", "error"]
    summary: str
    records: list[dict[str, Any]] = Field(default_factory=list)
    proposal: Optional[dict[str, Any]] = None
    record_count: int = 0
    # `record_count` is retained for compatibility and means total matching
    # records.  The explicit fields remove the historical total/page ambiguity.
    total_count: Optional[int] = Field(default=None, ge=0)
    returned_count: Optional[int] = Field(default=None, ge=0)
    requested_count: Optional[int] = Field(default=None, ge=0)
    offset: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list)
    provenance: list[str] = Field(default_factory=list)
    command_id: Optional[UUID] = None

    @model_validator(mode="after")
    def validate_count_relationships(self) -> "ToolResult":
        total = self.record_count if self.total_count is None else self.total_count
        returned = len(self.records) if self.returned_count is None else self.returned_count
        if returned != len(self.records):
            raise ValueError("returned_count must equal len(records)")
        if returned > total:
            raise ValueError("returned_count cannot exceed total_count")
        return self

    def normalized_counts(self) -> dict[str, int | None]:
        """Return backwards-compatible, unambiguous result-set counts."""
        total = self.record_count if self.total_count is None else self.total_count
        returned = len(self.records) if self.returned_count is None else self.returned_count
        return {
            "total_matching": total,
            "returned": returned,
            "requested": self.requested_count,
            "offset": self.offset,
        }
