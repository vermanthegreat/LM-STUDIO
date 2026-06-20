"""Deterministic read-tool handlers backed by ContactStore."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from repositories import ContactStore
from tools.envelope import ToolResult
from tools.read_inputs import (
    CalculatePipelineAnalyticsInput,
    FindCompaniesMissingEmailInput,
    ListDueFollowupsInput,
    MissingEmailDefinition,
    PipelineMetric,
    SearchContactsInput,
)


_SQLITE_VERIFICATION_WARNING = "sqlite_backend_verification_not_tracked"


def _sqlite_verification_warnings(store: ContactStore, *, uses_verified_semantics: bool) -> list[str]:
    if getattr(store, "backend", None) == "sqlite" and uses_verified_semantics:
        return [_SQLITE_VERIFICATION_WARNING]
    return []


def _lead_has_any_email(lead: dict[str, Any]) -> bool:
    email = (lead.get("company_email") or "").strip()
    if email:
        return True
    for person in lead.get("people") or []:
        if (person.get("email") or "").strip():
            return True
    return False


def _lead_has_email(
    lead: dict[str, Any],
    definition: MissingEmailDefinition,
    *,
    store: ContactStore,
) -> bool:
    if definition == MissingEmailDefinition.VERIFIED:
        if getattr(store, "backend", None) == "sqlite":
            return False
        return bool(lead.get("has_verified_email"))
    if definition == MissingEmailDefinition.NON_REJECTED:
        if getattr(store, "backend", None) == "sqlite":
            return _lead_has_any_email(lead)
        return bool(lead.get("has_non_rejected_email"))
    return _lead_has_any_email(lead)


def handle_search_contacts(store: ContactStore, args: BaseModel) -> ToolResult:
    params = SearchContactsInput.model_validate(args)
    leads = store.list_leads()
    records: list[dict[str, Any]] = []
    for lead in leads:
        if params.organization_status and lead.get("status") != params.organization_status:
            continue
        if params.minimum_relevance is not None and int(lead.get("fit_score") or 0) < params.minimum_relevance:
            continue
        if params.has_person is True and not lead.get("people_count"):
            continue
        if params.has_person is False and lead.get("people_count"):
            continue
        if params.text:
            needle = params.text.casefold()
            haystacks = [
                lead.get("company_name") or "",
                lead.get("website") or "",
                lead.get("company_email") or "",
            ]
            if not any(needle in value.casefold() for value in haystacks if value):
                continue
        records.append(lead)
    total = len(records)
    page = records[params.offset : params.offset + params.limit]
    return ToolResult(
        tool_name="search_contacts",
        status="ok",
        summary=f"Matched {total} organization(s).",
        records=page,
        record_count=total,
        provenance=["repository:list_leads"],
    )


def handle_find_companies_missing_email(store: ContactStore, args: BaseModel) -> ToolResult:
    params = FindCompaniesMissingEmailInput.model_validate(args)
    leads = store.list_leads()
    missing: list[dict[str, Any]] = []
    for lead in leads:
        if params.organization_status and lead.get("status") != params.organization_status:
            continue
        if params.minimum_relevance is not None and int(lead.get("fit_score") or 0) < params.minimum_relevance:
            continue
        detail = store.get_lead(lead["id"]) or lead
        if not _lead_has_email(detail, params.missing_definition, store=store):
            missing.append(lead)
    page = missing[: params.limit]
    warnings = [f"missing_definition={params.missing_definition.value}"]
    warnings.extend(
        _sqlite_verification_warnings(
            store,
            uses_verified_semantics=params.missing_definition == MissingEmailDefinition.VERIFIED,
        )
    )
    return ToolResult(
        tool_name="find_companies_missing_email",
        status="ok",
        summary=(
            f"Found {len(missing)} organization(s) with missing_definition="
            f"{params.missing_definition.value}."
        ),
        records=page,
        record_count=len(missing),
        provenance=["repository:list_leads", "repository:get_lead"],
        warnings=warnings,
    )


def handle_list_due_followups(store: ContactStore, args: BaseModel) -> ToolResult:
    params = ListDueFollowupsInput.model_validate(args)
    records = store.get_followups_due()
    filtered: list[dict[str, Any]] = []
    for item in records:
        if params.status and item.get("status") != params.status:
            continue
        if params.priority and item.get("priority") != params.priority:
            continue
        filtered.append(item)
    page = filtered[: params.limit]
    return ToolResult(
        tool_name="list_due_followups",
        status="ok",
        summary=f"Found {len(filtered)} follow-up task(s).",
        records=page,
        record_count=len(filtered),
        provenance=["repository:get_followups_due"],
    )


def handle_calculate_pipeline_analytics(store: ContactStore, args: BaseModel) -> ToolResult:
    params = CalculatePipelineAnalyticsInput.model_validate(args)
    summary = store.get_contact_summary()
    metric = params.metric
    warnings: list[str] = []
    if metric == PipelineMetric.ORGANIZATION_COUNT:
        value = int(summary.get("companies", 0))
        label = "organization_count"
    elif metric == PipelineMetric.CONTACT_COVERAGE:
        companies = int(summary.get("companies", 0))
        with_email = int(summary.get("with_any_email", 0))
        value = round((with_email / companies) * 100, 2) if companies else 0.0
        label = "contact_coverage_percent"
    elif metric == PipelineMetric.VERIFIED_EMAIL_COVERAGE:
        companies = int(summary.get("companies", 0))
        with_verified = int(summary.get("with_verified_email", 0))
        value = round((with_verified / companies) * 100, 2) if companies else 0.0
        label = "verified_email_coverage_percent"
        warnings.extend(_sqlite_verification_warnings(store, uses_verified_semantics=True))
    else:
        followups = store.get_followups_due()
        value = len(followups)
        label = "overdue_task_count"
    return ToolResult(
        tool_name="calculate_pipeline_analytics",
        status="ok",
        summary=f"{label}={value}",
        records=[{"metric": label, "value": value}],
        record_count=1,
        provenance=["repository:get_contact_summary", "repository:get_followups_due"],
        warnings=warnings,
    )
