"""Phase 3 write-proposal handlers — preview only, no direct mutation."""

from __future__ import annotations

from pydantic import BaseModel

from repositories import ContactStore
from tools.envelope import ToolResult
from tools.write_inputs import ContactUpdateField, ProposeCreateFollowupInput, ProposeContactUpdateInput


class WriteProposalError(Exception):
    """Business validation failure for a write proposal."""


def handle_propose_create_followup(store: ContactStore, args: BaseModel) -> ToolResult:
    params = ProposeCreateFollowupInput.model_validate(args)
    lead = store.get_lead(params.lead_id)
    if lead is None:
        raise WriteProposalError(f"Lead not found: {params.lead_id}")

    proposal = {
        "action": "create_followup",
        "lead_id": params.lead_id,
        "company_name": lead.get("company_name"),
        "title": params.title,
        "due_date": params.due_date,
        "priority": params.priority,
        "idempotency_key": params.idempotency_key,
    }
    summary_parts = [f'Proposed follow-up "{params.title}" for {lead.get("company_name")}']
    if params.due_date:
        summary_parts.append(f"due {params.due_date}")
    return ToolResult(
        tool_name="propose_create_followup",
        status="ok",
        summary=" ".join(summary_parts) + ". No changes have been made.",
        proposal=proposal,
        record_count=0,
        provenance=["repository:get_lead"],
    )


def handle_propose_contact_update(store: ContactStore, args: BaseModel) -> ToolResult:
    params = ProposeContactUpdateInput.model_validate(args)
    lead = store.get_lead(params.lead_id)
    if lead is None:
        raise WriteProposalError(f"Lead not found: {params.lead_id}")

    field_name = params.field.value
    previous_value = lead.get(field_name)
    proposal = {
        "action": "update_contact_field",
        "lead_id": params.lead_id,
        "company_name": lead.get("company_name"),
        "field": field_name,
        "value": params.value.strip(),
        "previous_value": previous_value,
        "idempotency_key": params.idempotency_key,
    }
    return ToolResult(
        tool_name="propose_contact_update",
        status="ok",
        summary=(
            f'Proposed update {field_name} for {lead.get("company_name")} '
            f'from "{previous_value or "n/a"}" to "{params.value.strip()}". '
            "No changes have been made."
        ),
        proposal=proposal,
        record_count=0,
        provenance=["repository:get_lead"],
    )
