"""Ask Database router for the FastAPI web app.

Flow:
1. Receive a free-form user question.
2. Map deterministic intents to typed read tools when registered.
3. Fall back to legacy repository intents, then optional LLM intent decode.
4. Return database-grounded results; optional LLM polish never invents data.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from uuid import UUID

import db
from llm import chat_completion, call_lmstudio_for_text
from repositories.sqlite_store import SqliteContactStore
from services.command_log import CommandLogError, CommandStatus
from services.command_service import CommandService
from tools.planner import PlannerToolCall
from tools.registry import ToolRegistryError, ToolValidationError, UnknownToolError

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")


@dataclass(frozen=True)
class AskIntent:
    name: str
    query: str = ""
    company: str = ""
    limit: int = 10
    filters: Dict[str, Any] | None = None
    confidence: float = 0.0

    @property
    def intent(self) -> str:
        return self.name


_ALLOWED_INTENTS = {
    "count_leads",
    "top_leads",
    "leads_without_contacts",
    "leads_without_email",
    "followups_due",
    "summarize_company",
    "contact_summary",
    "list_emails",
    "search_leads",
    "unknown",
}

_TOOL_ROUTED_INTENTS: dict[str, tuple[str, Any]] = {
    "leads_without_email": (
        "find_companies_missing_email",
        lambda intent: {"missing_definition": "any"},
    ),
    "followups_due": (
        "list_due_followups",
        lambda intent: {"limit": 50},
    ),
}

_STOPWORDS = {
    "and", "are", "for", "from", "that", "the", "with", "who", "which", "what",
    "find", "show", "list", "give", "lead", "leads", "agency", "agencies",
    "company", "companies", "koje", "koja", "koji", "rade", "radi", "agencije",
    "agencija", "kompanije", "klijenti", "daj", "nadji", "prikazi", "imamo", "ima",
    "bazi", "preko", "iznad", "mail", "email", "kontakt", "contact",
}


def answer_question(
    question: str,
    use_llm: bool = False,
    db_path=None,
    store=None,
    command_service: Optional[CommandService] = None,
    planner_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Answer a free-form Ask Database question from SQLite."""
    q = (question or "").strip()
    if not q:
        return {"question": q, "intent": "empty", "answer": "Unesi pitanje.", "data": None}

    if store is None:
        store = SqliteContactStore(db_path or db.DB_PATH)

    if planner_payload is not None:
        return execute_planner_tool_route(
            q,
            planner_payload,
            store=store,
            command_service=command_service,
        )

    intent = _deterministic_intent(q)

    if intent.name == "unknown" and use_llm:
        intent = _llm_intent(q)

    if intent.name == "unknown":
        intent = _fallback_search_intent(q)

    if intent.name in _TOOL_ROUTED_INTENTS:
        tool_name, args_builder = _TOOL_ROUTED_INTENTS[intent.name]
        tool_response = execute_tool_route(
            q,
            tool_name,
            args_builder(intent),
            store=store,
            command_service=command_service,
        )
        if tool_response.get("intent") != "tool_error":
            answer = tool_response["answer"]
            if use_llm and tool_response.get("data"):
                polished = _polish_answer(q, answer, tool_response["data"])
                if polished:
                    answer = polished
            return {
                **tool_response,
                "answer": answer,
            }

    result = _execute_intent(intent, store=store)
    answer = result["answer"]

    if use_llm and result.get("data"):
        polished = _polish_answer(q, answer, result["data"])
        if polished:
            answer = polished

    return {
        "question": q,
        "intent": result["intent"],
        "answer": answer,
        "data": result.get("data"),
    }


def _deterministic_intent(question: str) -> AskIntent:
    q = _norm(question)

    if any(term in q for term in ("how many", "koliko", "count", "broj")):
        if any(term in q for term in ("email", "contact", "kontakt")):
            return AskIntent("contact_summary", confidence=1.0)
        return AskIntent("count_leads", confidence=1.0)

    if any(term in q for term in (
        "all email", "list email", "print email", "show email", "email addresses",
        "emails from", "svi email", "prikazi email", "ispisi email",
    )):
        return AskIntent("list_emails", limit=_clamp_limit(_extract_limit(q, default=50), default=50, maximum=100), confidence=1.0)

    if any(term in q for term in (
        "contact summary", "contact info", "contact information", "available contact",
        "kontakt", "email coverage", "contact overview",
    )):
        return AskIntent("contact_summary", confidence=1.0)

    if any(term in q for term in ("without email", "no email", "missing email", "bez email", "bez maila")):
        return AskIntent("leads_without_email", confidence=1.0)

    if any(term in q for term in ("top leads", "best leads", "najbolj", "top klijenti", "top leadove")):
        return AskIntent("top_leads", limit=_extract_limit(q), confidence=1.0)

    if "without contact" in q or "bez kontakt" in q:
        return AskIntent("leads_without_contacts", confidence=1.0)

    if "follow" in q or "deadline" in q or "rok" in q:
        return AskIntent("followups_due", confidence=1.0)

    for prefix in ("summarize company ", "summary company ", "rezimiraj kompaniju ", "sumiraj kompaniju "):
        if q.startswith(prefix):
            return AskIntent("summarize_company", company=question[len(prefix):].strip(), confidence=1.0)

    if any(term in q for term in (
        "product description", "opis proizvoda", "opisi proizvoda", "seo", "copywriting", "content",
        "email", "mail", "contact", "kontakt",
    )):
        return AskIntent(
            "search_leads",
            query=question,
            filters=_basic_filters(q),
            limit=_extract_limit(q),
            confidence=0.75,
        )

    return AskIntent("unknown")


def _llm_intent(question: str) -> AskIntent:
    prompt = f"""Classify this question for a local SQLite leads database.
Return JSON only. No prose. No SQL.

Allowed intents:
- count_leads
- top_leads
- leads_without_contacts
- leads_without_email
- followups_due
- summarize_company
- contact_summary
- list_emails
- search_leads
- unknown

Use contact_summary for questions about available contact info, email coverage, or company/contact counts together.
Use list_emails for listing stored email addresses.

Use search_leads for free-form filtering by service, location, industry, partner tier, score, status, company text, description, email/contact requests, or contact availability.

Available lead fields: company_name, website, company_email, partner_tier, rating, review_count, primary_location, services, locations, industries, description, fit_score, status, people_count, has_decision_maker.
Raw pasted sources can also contain email addresses even when company_email is empty.

JSON schema:
{{
  "intent": "search_leads",
  "query": "product descriptions",
  "company": null,
  "limit": 10,
  "filters": {{
    "services": ["product descriptions"],
    "locations": [],
    "industries": [],
    "partner_tier": null,
    "status": null,
    "min_fit_score": null,
    "has_contacts": null,
    "has_decision_maker": null
  }},
  "confidence": 0.9
}}

Question: {question!r}"""
    raw = call_lmstudio_for_text(prompt, timeout_s=8.0)
    if not raw:
        return AskIntent("unknown")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return AskIntent("unknown")

    if not isinstance(payload, dict):
        return AskIntent("unknown")

    name = str(payload.get("intent") or "unknown").strip().lower()
    if name not in _ALLOWED_INTENTS:
        name = "unknown"

    try:
        confidence = float(payload.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0

    if confidence < 0.45:
        return AskIntent("unknown")

    filters = payload.get("filters") if isinstance(payload.get("filters"), dict) else {}
    return AskIntent(
        name=name,
        query=str(payload.get("query") or "").strip(),
        company=str(payload.get("company") or "").strip(),
        limit=_clamp_limit(payload.get("limit")),
        filters=filters,
        confidence=confidence,
    )


def _fallback_search_intent(question: str) -> AskIntent:
    return AskIntent(
        "search_leads",
        query=question,
        filters=_basic_filters(_norm(question)),
        limit=_extract_limit(question),
        confidence=0.50,
    )


def _execute_intent(intent: AskIntent, *, store) -> Dict[str, Any]:
    if intent.name == "count_leads":
        count = store.count_potential_clients()
        return {"intent": intent.name, "answer": f"Imamo {count} potencijalnih klijenata u bazi.", "data": {"count": count}}

    if intent.name == "top_leads":
        leads = store.get_top_leads(intent.limit)
        enriched = _attach_contact_fields(leads, store=store)
        return {
            "intent": intent.name,
            "answer": _format_leads(enriched, "Top leads:"),
            "data": {"leads": enriched},
        }

    if intent.name == "leads_without_contacts":
        leads = store.get_leads_without_contacts()
        enriched = _attach_contact_fields(leads, store=store)
        return {
            "intent": intent.name,
            "answer": _format_leads(enriched, "Leads without contacts:"),
            "data": {"leads": enriched},
        }

    if intent.name == "leads_without_email":
        leads = store.get_leads_without_email()
        return {"intent": intent.name, "answer": _format_leads(leads, "Companies missing email:"), "data": {"leads": leads}}

    if intent.name == "contact_summary":
        summary = store.get_contact_summary()
        lines = [
            "Contact overview:",
            f"- Companies: {summary['companies']}",
            f"- With company email: {summary['with_company_email']}",
            f"- With people on file: {summary['with_people']}",
            f"- With person email: {summary['with_person_email']}",
            f"- With any email (required): {summary['with_any_email']}",
            f"- Missing email: {summary['without_email']}",
            f"- Saved email threads: {summary['email_interactions']}",
        ]
        return {"intent": intent.name, "answer": "\n".join(lines), "data": summary}

    if intent.name == "list_emails":
        rows = store.list_contact_emails(intent.limit)
        return {"intent": intent.name, "answer": _format_emails(rows, intent.limit), "data": {"emails": rows}}

    if intent.name == "followups_due":
        items = store.get_followups_due()
        lines = ["Follow-ups due:"]
        for item in items[:10]:
            lines.append(f"- {item.get('company_name')}: {item.get('title') or item.get('subject')} (due {item.get('due_date')})")
        if len(lines) == 1:
            lines.append("(none)")
        return {"intent": intent.name, "answer": "\n".join(lines), "data": {"items": items}}

    if intent.name == "summarize_company":
        if not intent.company:
            return _unknown(store=store)
        rows = store.search_lead_by_name(intent.company)
        if not rows:
            return {"intent": intent.name, "answer": f"Nema leada za '{intent.company}'.", "data": {}}
        lead = store.get_lead(rows[0]["id"])
        lead = _attach_contact_fields([lead], store=store)[0] if lead else lead
        return {"intent": intent.name, "answer": _summarize_lead(lead), "data": {"lead": lead}}

    if intent.name == "search_leads":
        leads = store.list_leads()
        matches = _search_leads(leads, intent)
        hydrated = _attach_contact_fields(matches, store=store)
        return {
            "intent": intent.name,
            "answer": _format_search(hydrated, intent),
            "data": {"leads": hydrated, "parsed_query": intent.query, "filters": intent.filters or {}, "confidence": intent.confidence},
        }

    return _unknown(store=store)


def execute_tool_route(
    question: str,
    tool_name: str,
    arguments: Dict[str, Any],
    *,
    store,
    command_service: Optional[CommandService] = None,
) -> Dict[str, Any]:
    """Execute a registered read tool with command-log audit for /ask routing."""
    service = command_service or CommandService(store)
    entry = service.receive(question)
    try:
        service.registry.get(tool_name)
        validated = service.registry.validate_arguments(tool_name, arguments)
        payload = validated.model_dump(mode="json")
    except (UnknownToolError, ToolValidationError) as exc:
        service.reject(entry, code=type(exc).__name__, message=str(exc))
        return {
            "question": question,
            "intent": "tool_error",
            "answer": str(exc),
            "data": {
                "command_id": str(entry.id),
                "command_status": entry.status.value,
                "tool_name": tool_name,
            },
        }

    service.plan_tool_call(
        entry,
        PlannerToolCall(
            tool_name=tool_name,
            arguments=payload,
            reason="Deterministic /ask route.",
        ),
    )
    try:
        result = service.execute_planned_tool(entry)
    except ToolRegistryError as exc:
        updated = service.get_command(entry.id)
        return {
            "question": question,
            "intent": "tool_error",
            "answer": str(exc),
            "data": {
                "command_id": str(entry.id),
                "command_status": updated.status.value if updated else "failed",
                "tool_name": tool_name,
            },
        }

    return _tool_result_to_ask_response(question, tool_name, result, entry)


def execute_planner_tool_route(
    question: str,
    payload: Dict[str, Any],
    *,
    store,
    command_service: Optional[CommandService] = None,
) -> Dict[str, Any]:
    """Dispatch one validated planner tool call to read-only or proposal-only paths."""
    service = command_service or CommandService(store)
    tool_name = payload.get("tool_name")
    if isinstance(tool_name, str) and tool_name:
        try:
            spec = service.registry.get(tool_name)
            if spec.risk_class.creates_write_proposal:
                return execute_planner_propose_tool_route(
                    question,
                    payload,
                    store=store,
                    command_service=service,
                )
        except UnknownToolError:
            pass
    return execute_planner_read_tool_route(
        question,
        payload,
        store=store,
        command_service=service,
    )


def execute_planner_read_tool_route(
    question: str,
    payload: Dict[str, Any],
    *,
    store,
    command_service: Optional[CommandService] = None,
) -> Dict[str, Any]:
    """Execute one validated read-only planner tool call for /ask."""
    from services.planner_validation import execute_planner_read_tool_call

    service = command_service or CommandService(store)
    outcome = execute_planner_read_tool_call(
        service,
        command_text=question,
        payload=payload,
    )
    if outcome["status"] != "ok":
        return {
            "question": question,
            "intent": outcome["intent"],
            "answer": outcome["message"],
            "data": outcome["data"],
        }
    return _tool_result_to_ask_response(
        question,
        outcome["tool_name"],
        outcome["result"],
        outcome["entry"],
    )


def execute_planner_propose_tool_route(
    question: str,
    payload: Dict[str, Any],
    *,
    store,
    command_service: Optional[CommandService] = None,
) -> Dict[str, Any]:
    """Preview one validated propose-class planner tool call for /ask."""
    from services.planner_validation import execute_planner_propose_tool_call

    service = command_service or CommandService(store)
    outcome = execute_planner_propose_tool_call(
        service,
        command_text=question,
        payload=payload,
    )
    if outcome["status"] != "ok":
        return {
            "question": question,
            "intent": outcome["intent"],
            "answer": outcome["message"],
            "data": outcome["data"],
        }
    return _proposal_result_to_ask_response(
        question,
        outcome["tool_name"],
        outcome["result"],
        outcome["entry"],
    )


def approve_write_proposal_route(
    command_id: UUID,
    *,
    store,
    command_service: Optional[CommandService] = None,
) -> Dict[str, Any]:
    """Record explicit approval for a command awaiting write apply."""
    service = command_service or CommandService(store)
    entry = service.get_command(command_id)
    if entry is None:
        return _command_route_error(
            command_id,
            error_code="command_not_found",
            message=f"Command {command_id} was not found.",
        )
    try:
        service.approve_command(entry)
    except CommandLogError as exc:
        return _command_route_error(
            command_id,
            error_code=type(exc).__name__,
            message=str(exc),
            command_status=entry.status.value,
            tool_name=entry.tool_name,
        )
    updated = service.get_command(command_id)
    assert updated is not None
    return {
        "status": "ok",
        "intent": "write_proposal_approved",
        "answer": "Write proposal approved. Apply to commit the change.",
        "data": {
            "command_id": str(command_id),
            "command_status": updated.status.value,
            "tool_name": updated.tool_name,
            "requires_approval": updated.requires_approval,
            "approved_at": updated.approved_at.isoformat() if updated.approved_at else None,
        },
    }


def apply_write_proposal_route(
    command_id: UUID,
    *,
    store,
    command_service: Optional[CommandService] = None,
) -> Dict[str, Any]:
    """Apply an approved write proposal from command_log."""
    service = command_service or CommandService(store)
    entry = service.get_command(command_id)
    if entry is None:
        return _command_route_error(
            command_id,
            error_code="command_not_found",
            message=f"Command {command_id} was not found.",
        )
    try:
        result = service.apply_approved_command(entry)
    except CommandLogError as exc:
        updated = service.get_command(command_id)
        return _command_route_error(
            command_id,
            error_code=type(exc).__name__,
            message=str(exc),
            command_status=updated.status.value if updated else entry.status.value,
            tool_name=entry.tool_name,
        )

    updated = service.get_command(command_id)
    assert updated is not None
    return {
        "status": "ok",
        "intent": "write_applied",
        "answer": result.summary,
        "data": {
            "command_id": str(command_id),
            "command_status": updated.status.value,
            "tool_name": updated.tool_name,
            "record_count": result.record_count,
            "records": result.records,
        },
    }


def _command_route_error(
    command_id: UUID,
    *,
    error_code: str,
    message: str,
    command_status: Optional[str] = None,
    tool_name: Optional[str] = None,
) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "command_id": str(command_id),
        "error_code": error_code,
    }
    if command_status is not None:
        data["command_status"] = command_status
    if tool_name is not None:
        data["tool_name"] = tool_name
    return {
        "status": "error",
        "intent": "write_proposal_error",
        "answer": message,
        "data": data,
    }


def _proposal_result_to_ask_response(
    question: str,
    tool_name: str,
    result,
    entry,
) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "command_id": str(entry.id),
        "command_status": entry.status.value,
        "tool_name": tool_name,
        "requires_approval": entry.requires_approval,
        "proposal": result.proposal,
        "record_count": 0,
    }
    if tool_name == "propose_create_followup":
        return {
            "question": question,
            "intent": "write_proposal",
            "answer": (
                f"{result.summary}\n"
                "No changes have been made. Approve this proposal before applying."
            ),
            "data": data,
        }
    return {
        "question": question,
        "intent": "write_proposal",
        "answer": result.summary,
        "data": data,
    }


def _tool_result_to_ask_response(
    question: str,
    tool_name: str,
    result,
    entry,
) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "command_id": str(entry.id),
        "command_status": entry.status.value,
        "tool_name": tool_name,
        "record_count": result.record_count,
    }
    if tool_name == "find_companies_missing_email":
        data["leads"] = result.records
        return {
            "question": question,
            "intent": "leads_without_email",
            "answer": _format_leads(result.records, "Companies missing email:"),
            "data": data,
        }
    if tool_name == "list_due_followups":
        data["items"] = result.records
        lines = ["Follow-ups due:"]
        for item in result.records[:10]:
            lines.append(
                f"- {item.get('company_name')}: {item.get('title') or item.get('subject')} "
                f"(due {item.get('due_date')})"
            )
        if len(lines) == 1:
            lines.append("(none)")
        return {
            "question": question,
            "intent": "followups_due",
            "answer": "\n".join(lines),
            "data": data,
        }
    data["records"] = result.records
    return {
        "question": question,
        "intent": tool_name,
        "answer": result.summary,
        "data": data,
    }


def _search_leads(leads: List[Dict[str, Any]], intent: AskIntent) -> List[Dict[str, Any]]:
    filters = intent.filters or {}
    terms = _terms(intent.query)
    scored: list[tuple[int, int, str, Dict[str, Any]]] = []

    for lead in leads:
        if not _passes_filters(lead, filters):
            continue
        blob = _blob(lead)
        score = sum(3 if " " in term else 1 for term in terms if term in blob)
        for value in _filter_terms(filters):
            if value in blob:
                score += 4
        if terms and score <= 0 and not filters:
            continue
        scored.append((score, int(lead.get("fit_score") or 0), str(lead.get("company_name") or "").lower(), lead))

    scored.sort(key=lambda row: (-row[0], -row[1], row[2]))
    return [row[3] for row in scored[: intent.limit]]


def _passes_filters(lead: Dict[str, Any], filters: Dict[str, Any]) -> bool:
    min_fit = filters.get("min_fit_score")
    if min_fit not in (None, ""):
        try:
            if int(lead.get("fit_score") or 0) < int(min_fit):
                return False
        except (TypeError, ValueError):
            pass

    if not _matches_any(lead, "services", filters.get("services") or filters.get("service")):
        return False
    if not _matches_any(lead, "locations", filters.get("locations") or filters.get("location"), extra=lead.get("primary_location")):
        return False
    if not _matches_any(lead, "industries", filters.get("industries") or filters.get("industry")):
        return False

    partner_tier = filters.get("partner_tier")
    if partner_tier:
        wanted = _norm(str(partner_tier))
        actual = _norm(str(lead.get("partner_tier") or ""))
        if wanted not in actual and not ("plus" in wanted and "plus" in actual):
            return False

    status = filters.get("status")
    if status and _norm(str(status)) not in _norm(str(lead.get("status") or "")):
        return False

    has_contacts = filters.get("has_contacts")
    if has_contacts is not None and bool(has_contacts) != (int(lead.get("people_count") or 0) > 0):
        return False

    has_dm = filters.get("has_decision_maker")
    if has_dm is not None and bool(has_dm) != bool(lead.get("has_decision_maker")):
        return False

    return True


def _matches_any(lead: Dict[str, Any], key: str, wanted: Any, *, extra: Any = None) -> bool:
    wanted_values = _as_list(wanted)
    if not wanted_values:
        return True
    haystack = []
    value = lead.get(key)
    if isinstance(value, list):
        haystack.extend(str(item) for item in value)
    elif value:
        haystack.append(str(value))
    if extra:
        haystack.append(str(extra))
    text = _norm(" ".join(haystack))
    return any(_norm(item) in text for item in wanted_values)


def _basic_filters(q: str) -> Dict[str, Any]:
    filters: Dict[str, Any] = {}
    services = []
    if "product description" in q or "opis proizvoda" in q or "opisi proizvoda" in q:
        services.append("product descriptions")
    if "seo" in q:
        services.append("seo")
    if "copywriting" in q or "copy" in q:
        services.append("copywriting")
    if "content" in q or "sadrzaj" in q or "sadržaj" in q:
        services.append("content")
    if services:
        filters["services"] = services
    score = re.search(r"(?:score|fit|preko|iznad)\D{0,12}(\d{1,3})", q)
    if score:
        filters["min_fit_score"] = int(score.group(1))
    return filters


def _attach_contact_fields(leads: List[Dict[str, Any]], *, store) -> List[Dict[str, Any]]:
    result = []
    for lead in leads:
        enriched = dict(lead or {})
        lead_id = enriched.get("id")
        detail = None
        if lead_id is not None:
            try:
                detail = store.get_lead(int(lead_id))
            except Exception:
                detail = None
        if detail:
            enriched.update({k: v for k, v in detail.items() if k not in enriched or enriched.get(k) in (None, "", [], {})})
        emails = _collect_emails(enriched)
        enriched["emails"] = emails
        enriched["email_display"] = ", ".join(emails) if emails else "n/a"
        result.append(enriched)
    return result


def _collect_emails(lead: Dict[str, Any]) -> List[str]:
    found: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        if not value:
            return
        for match in EMAIL_RE.findall(str(value)):
            low = match.lower()
            if low.endswith(("shopify.com", "myshopify.com")) or low in seen:
                continue
            seen.add(low)
            found.append(match)

    add(lead.get("company_email"))
    for source in lead.get("raw_sources") or []:
        if isinstance(source, dict):
            add(source.get("raw_text"))
            add(source.get("parsed_json"))
    return found


def _format_emails(rows: List[Dict[str, Any]], limit: int) -> str:
    header = "Stored email addresses:"
    if not rows:
        return f"{header}\n(none)"
    lines = [header]
    for row in rows[:limit]:
        company = row.get("company_name") or "?"
        email = row.get("email") or "?"
        if row.get("source") == "person":
            who = row.get("person_name") or "contact"
            lines.append(f"- {company} | {who} <{email}>")
        else:
            lines.append(f"- {company} | {email}")
    return "\n".join(lines)


def _format_leads(leads: List[Dict[str, Any]], header: str) -> str:
    if not leads:
        return f"{header}\n(none)"
    lines = [header]
    for lead in leads[:20]:
        lines.append(
            f"- {lead.get('company_name') or '?'} "
            f"(fit={lead.get('fit_score', 0)}, status={lead.get('status')}, email={lead.get('email_display')})"
        )
    return "\n".join(lines)


def _format_search(leads: List[Dict[str, Any]], intent: AskIntent) -> str:
    header = f"Search results for: {intent.query or intent.filters or 'lead search'}"
    if not leads:
        return f"{header}\n(none)"
    lines = [header]
    for lead in leads[: intent.limit]:
        services = lead.get("services") if isinstance(lead.get("services"), list) else []
        locations = lead.get("locations") if isinstance(lead.get("locations"), list) else []
        service_text = ", ".join(services[:3]) if services else "n/a"
        location = lead.get("primary_location") or (", ".join(locations[:2]) if locations else "n/a")
        lines.append(
            f"- {lead.get('company_name') or '?'} "
            f"(fit={lead.get('fit_score', 0)}, status={lead.get('status')}, "
            f"tier={lead.get('partner_tier') or 'n/a'}, location={location}, "
            f"services={service_text}, website={lead.get('website') or 'n/a'}, "
            f"email={lead.get('email_display') or 'n/a'})"
        )
    return "\n".join(lines)


def _summarize_lead(lead: Optional[Dict[str, Any]]) -> str:
    if not lead:
        return "Lead not found."
    people = lead.get("people") or []
    services = lead.get("services") or []
    lines = [
        f"**{lead.get('company_name')}**",
        f"Website: {lead.get('website') or 'n/a'}",
        f"Email: {lead.get('email_display') or 'n/a'}",
        f"Partner tier: {lead.get('partner_tier') or 'n/a'}",
        f"Fit score: {lead.get('fit_score', 0)}",
        f"Status: {lead.get('status')}",
        f"Services: {', '.join(services) if services else 'n/a'}",
        f"People: {len(people)}",
        f"Interactions: {len(lead.get('interactions') or [])}",
    ]
    if lead.get("description"):
        lines.append(f"Description: {lead['description'][:300]}")
    return "\n".join(lines)


def _unknown(*, store) -> Dict[str, Any]:
    count = store.count_potential_clients()
    return {
        "intent": "unknown",
        "answer": f"Nisam siguran na pitanje. Trenutno imamo {count} potencijalnih klijenata.",
        "data": {"count": count},
    }


def _polish_answer(question: str, answer: str, data: Dict[str, Any]) -> Optional[str]:
    return chat_completion(
        [
            {"role": "system", "content": "Summarize database results. Use only provided data. Do not invent. Preserve email fields exactly."},
            {"role": "user", "content": f"Question: {question}\n\nAnswer draft:\n{answer}\n\nData:\n{data}"},
        ],
        temperature=0.0,
        max_tokens=1200,
    )


def _filter_terms(filters: Dict[str, Any]) -> List[str]:
    values: list[str] = []
    for key in ("services", "service", "locations", "location", "industries", "industry", "partner_tier", "status"):
        values.extend(_norm(item) for item in _as_list(filters.get(key)))
    return values


def _blob(lead: Dict[str, Any]) -> str:
    values: list[str] = []
    for key in ("company_name", "website", "company_email", "partner_tier", "primary_location", "description", "status"):
        if lead.get(key):
            values.append(str(lead.get(key)))
    for key in ("services", "locations", "industries", "supported_locations", "languages"):
        value = lead.get(key)
        if isinstance(value, list):
            values.extend(str(item) for item in value)
    return _norm(" ".join(values))


def _terms(text: str) -> List[str]:
    q = _norm(text)
    phrases = [phrase for phrase in ("product descriptions", "product description", "shopify plus") if phrase in q]
    words = [word for word in re.findall(r"[a-z0-9ćčžšđ]+", q) if len(word) >= 3 and word not in _STOPWORDS]
    return phrases + [word for word in words if word not in phrases][:8]


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _extract_limit(text: str, default: int = 10) -> int:
    match = re.search(r"\b(\d{1,2})\b", text)
    return _clamp_limit(match.group(1) if match else None, default=default)


def _clamp_limit(value: Any, default: int = 10, maximum: int = 25) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, maximum))


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text).lower()).strip()


def route_question(question: str, db_path=None, use_llm: bool = True, store=None) -> Dict[str, Any]:
    """Classify question and run SQLite query. Returns structured result."""
    q = (question or "").strip()
    if store is None:
        store = SqliteContactStore(db_path or db.DB_PATH)
    intent = _deterministic_intent(q)
    if intent.name == "unknown" and use_llm:
        intent = _llm_intent(q)
    if intent.name == "unknown":
        intent = _fallback_search_intent(q)
    result = _execute_intent(intent, store=store)
    return {**result, "question": q}


def deterministic_ask_intent(question: str) -> Optional[AskIntent]:
    intent = _deterministic_intent(question)
    if intent.name == "unknown":
        return None
    return intent


def parse_llm_intent_payload(raw_text: str) -> AskIntent:
    try:
        payload = json.loads((raw_text or "").strip())
    except json.JSONDecodeError:
        return AskIntent("unknown")

    if not isinstance(payload, dict):
        return AskIntent("unknown")

    name = str(payload.get("intent") or "unknown").strip().lower()
    if name not in _ALLOWED_INTENTS:
        name = "unknown"

    try:
        confidence = float(payload.get("confidence") if payload.get("confidence") is not None else 1.0)
    except (TypeError, ValueError):
        confidence = 0.0

    if confidence < 0.60 and name != "unknown":
        name = "unknown"

    filters = payload.get("filters") if isinstance(payload.get("filters"), dict) else {}
    return AskIntent(
        name=name,
        query=str(payload.get("query") or "").strip(),
        company=str(payload.get("company") or "").strip(),
        limit=_clamp_limit(payload.get("limit")),
        filters=filters,
        confidence=confidence,
    )
