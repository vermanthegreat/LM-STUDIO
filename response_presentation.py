"""Validated, application-owned presentation for Ask tool results.

The model may eventually request a response specification, but this module
owns allowed fields, row values, ordering, counts, and the safe fallback.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tools.envelope import ToolResult


class ResponseColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=60)
    format: Literal["text", "number", "date", "datetime", "email", "url", "verification", "status"] = "text"


class ResponseSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["table", "cards", "timeline", "brief", "draft", "proposal_preview"]
    title: str = Field(min_length=1, max_length=120)
    introduction: str | None = Field(default=None, max_length=600)
    columns: list[ResponseColumn] = Field(default_factory=list, max_length=12)
    group_by: str | None = Field(default=None, max_length=80)
    null_display: Literal["n/a", "—", "blank"] = "n/a"
    include_provenance: bool = True
    include_warnings: bool = True


_TOOL_PRESENTATIONS: dict[str, tuple[str, ResponseSpec, set[str]]] = {
    "find_companies_missing_email": (
        "company",
        ResponseSpec(mode="table", title="Companies missing email", columns=[
            ResponseColumn(field="company_name", label="Company"),
            ResponseColumn(field="fit_score", label="Fit", format="number"),
            ResponseColumn(field="status", label="Status", format="status"),
            ResponseColumn(field="company_email", label="Email", format="email"),
        ]),
        {"id", "company_name", "fit_score", "status", "company_email", "website"},
    ),
    "search_contacts": (
        "company",
        ResponseSpec(mode="table", title="Contact search results", columns=[
            ResponseColumn(field="company_name", label="Company"),
            ResponseColumn(field="company_email", label="Email", format="email"),
            ResponseColumn(field="status", label="Status", format="status"),
        ]),
        {"id", "company_name", "company_email", "status", "fit_score", "website"},
    ),
    "list_due_followups": (
        "task",
        ResponseSpec(mode="table", title="Follow-ups due", columns=[
            ResponseColumn(field="company_name", label="Company"),
            ResponseColumn(field="title", label="Task"),
            ResponseColumn(field="due_date", label="Due", format="date"),
            ResponseColumn(field="priority", label="Priority", format="status"),
        ]),
        {"id", "company_name", "title", "subject", "due_date", "priority", "status", "item_type"},
    ),
    "list_unverified_contact_methods": (
        "contact_method",
        ResponseSpec(mode="table", title="Unverified contact methods", columns=[
            ResponseColumn(field="company_name", label="Company"),
            ResponseColumn(field="person_name", label="Person"),
            ResponseColumn(field="kind", label="Kind"),
            ResponseColumn(field="value", label="Value"),
            ResponseColumn(field="verification_status", label="Verification", format="verification"),
        ]),
        {"lead_id", "company_name", "person_name", "kind", "value", "verification_status"},
    ),
    "list_email_messages": (
        "email",
        ResponseSpec(mode="table", title="Imported email messages", columns=[
            ResponseColumn(field="occurred_at_local", label="Received", format="datetime"),
            ResponseColumn(field="subject", label="Subject"),
            ResponseColumn(field="direction", label="Direction", format="status"),
            ResponseColumn(field="link_status", label="Link", format="status"),
        ]),
        {"id", "external_thread_id", "occurred_at_local", "subject", "direction", "link_status", "primary_intent", "message_role"},
    ),
    "get_email_thread": (
        "email",
        ResponseSpec(mode="timeline", title="Imported email thread", columns=[
            ResponseColumn(field="occurred_at_local", label="Time", format="datetime"),
            ResponseColumn(field="subject", label="Subject"),
            ResponseColumn(field="direction", label="Direction", format="status"),
        ]),
        {"id", "external_thread_id", "occurred_at_local", "subject", "direction", "body_preview", "markers"},
    ),
}


def default_response_spec(tool_name: str) -> tuple[str, ResponseSpec, set[str]]:
    return _TOOL_PRESENTATIONS.get(
        tool_name,
        ("record", ResponseSpec(mode="brief", title="Database result"), set()),
    )


def validate_response_spec(tool_name: str, raw_spec: ResponseSpec | dict[str, Any] | None) -> tuple[ResponseSpec, list[str]]:
    """Validate a presentation request and safely fall back on any violation."""
    _, fallback, allowed = default_response_spec(tool_name)
    if raw_spec is None:
        return fallback, []
    try:
        spec = raw_spec if isinstance(raw_spec, ResponseSpec) else ResponseSpec.model_validate(raw_spec)
        requested_fields = {column.field for column in spec.columns}
        if not allowed and requested_fields:
            raise ValueError("response columns are not allowed for this tool")
        if allowed and not requested_fields <= allowed:
            raise ValueError("response columns are not allowed for this tool")
        if spec.group_by and allowed and spec.group_by not in allowed:
            raise ValueError("response group field is not allowed for this tool")
    except (ValidationError, ValueError, TypeError):
        return fallback, ["response_spec_invalid"]
    return spec, []


def presentation_data(tool_name: str, result: ToolResult, raw_spec: ResponseSpec | dict[str, Any] | None = None) -> dict[str, Any]:
    """Produce JSON-safe presentation metadata without changing result rows."""
    entity_type, _, _ = default_response_spec(tool_name)
    spec, warnings = validate_response_spec(tool_name, raw_spec)
    return {
        "entity_type": entity_type,
        "response_spec": spec.model_dump(mode="json"),
        "result_metadata": result.normalized_counts(),
        "presentation_warnings": warnings,
    }


def render_plain_text(tool_name: str, result: ToolResult, raw_spec: ResponseSpec | dict[str, Any] | None = None) -> str:
    """Render a deterministic compatibility projection for existing JSON clients."""
    spec, _ = validate_response_spec(tool_name, raw_spec)
    counts = result.normalized_counts()
    lines = [
        f"{spec.title}: requested {counts['requested']}, returned {counts['returned']}, "
        f"total matching {counts['total_matching']}, offset {counts['offset']}."
    ]
    for row in result.records:
        values = [str(row.get(column.field) if row.get(column.field) not in (None, "") else spec.null_display) for column in spec.columns]
        if values:
            lines.append(" | ".join(values))
    if not result.records:
        lines.append("(none)")
    return "\n".join(lines)
