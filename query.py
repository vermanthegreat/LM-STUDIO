"""Natural-language database queries — SQLite first, optional LLM summary."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Tuple

import db
from llm import chat_completion, call_lmstudio_for_text

AskIntentName = Literal[
    "count_leads",
    "top_leads",
    "leads_without_contacts",
    "followups_due",
    "summarize_company",
    "unknown",
]


ALLOWED_ASK_DATABASE_INTENTS: set[str] = {
    "count_leads",
    "top_leads",
    "leads_without_contacts",
    "followups_due",
    "summarize_company",
    "unknown",
}


@dataclass(frozen=True)
class AskDatabaseIntent:
    intent: AskIntentName
    company: str | None = None
    limit: int = 10
    filters: dict[str, Any] | None = None
    confidence: float = 0.0


def _normalize_q(question: str) -> str:
    q = question.strip().lower()
    q = q.replace("\xa0", " ").replace("\u200b", "")
    q = re.sub(r"\s+", " ", q)
    return q.rstrip(".?!,")


def normalize_ask_question(question: str) -> str:
    """Normalize question for intent routing."""
    return _normalize_q(question)


def clamp_ask_limit(value: Any, *, default: int = 10, maximum: int = 25) -> int:
    """Clamp limit value to valid range."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, maximum))


def parse_llm_intent_payload(raw_text: str) -> AskDatabaseIntent:
    """Parse LLM output into validated AskDatabaseIntent."""
    try:
        payload = json.loads((raw_text or "").strip())
    except json.JSONDecodeError:
        return AskDatabaseIntent(intent="unknown")

    if not isinstance(payload, dict):
        return AskDatabaseIntent(intent="unknown")

    intent = str(payload.get("intent") or "unknown").strip().lower()
    if intent not in ALLOWED_ASK_DATABASE_INTENTS:
        intent = "unknown"

    try:
        confidence = float(payload.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0

    if confidence < 0.60 and intent != "unknown":
        intent = "unknown"

    company = payload.get("company")
    if company is not None:
        company = str(company).strip() or None

    filters = payload.get("filters")
    if not isinstance(filters, dict):
        filters = {}

    return AskDatabaseIntent(
        intent=intent,  # type: ignore[arg-type]
        company=company,
        limit=clamp_ask_limit(payload.get("limit")),
        filters=filters,
        confidence=confidence,
    )


_TOP_LEADS_PHRASES = (
    "show top leads",
    "top leads",
    "show best leads",
    "best leads",
    "najbolji leadovi",
)


def deterministic_ask_intent(question: str) -> AskDatabaseIntent | None:
    """Route known UI examples deterministically without LM Studio."""
    q = normalize_ask_question(question)

    if q in {
        "koliko imamo potencijalnih klijenata?",
        "koliko imamo potencijalnih klijenata",
        "how many leads",
        "how many potential clients",
        "count leads",
    }:
        return AskDatabaseIntent(intent="count_leads", confidence=1.0)

    if q in {
        "show top leads",
        "top leads",
        "show best leads",
        "best leads",
        "prikazi top leadove",
        "prikaži top leadove",
        "top klijenti",
    }:
        return AskDatabaseIntent(intent="top_leads", limit=10, confidence=1.0)

    if q in {
        "show leads without contacts",
        "leads without contacts",
        "leadovi bez kontakata",
        "klijenti bez kontakata",
    }:
        return AskDatabaseIntent(intent="leads_without_contacts", confidence=1.0)

    if q in {
        "show follow-ups due",
        "follow-ups due",
        "followups due",
        "follow ups due",
        "follow-upovi za danas",
    }:
        return AskDatabaseIntent(intent="followups_due", confidence=1.0)

    summarize_prefixes = (
        "summarize company ",
        "summary company ",
        "rezimiraj kompaniju ",
        "sumiraj kompaniju ",
    )
    for prefix in summarize_prefixes:
        if q.startswith(prefix):
            company = question[len(prefix):].strip()
            if company:
                return AskDatabaseIntent(
                    intent="summarize_company",
                    company=company,
                    confidence=1.0,
                )

    return None


def _is_top_leads_query(q: str) -> bool:
    if q in _TOP_LEADS_PHRASES:
        return True
    return any(
        phrase in q
        for phrase in (
            "top leads",
            "best leads",
            "top lead",
            "best lead",
            "najbolji leadovi",
        )
    )


def route_question(question: str, db_path=None) -> Dict[str, Any]:
    """Classify question and run SQLite query. Returns structured result."""
    q = _normalize_q(question)
    kwargs = {}
    if db_path is not None:
        kwargs["db_path"] = db_path

    # Deterministic routing for known UI examples first
    deterministic = deterministic_ask_intent(question)
    if deterministic is not None:
        return _execute_intents_from_dict(deterministic, **kwargs)

    # Fallback to LLM intent classification
    result = route_with_llm_intent(question, db_path=db_path)
    if result is not None and "intent" in result:
        return result

    # Unknown - use fallback
    count = db.count_potential_clients(**kwargs)
    return {
        "intent": "unknown",
        "data": {"count": count},
        "answer": (
            f"Nisam siguran na pitanje. Trenutno imamo {count} potencijalnih klijenata. "
            "Probaj: 'show top leads', 'show leads without contacts', "
            "'show follow-ups due', 'summarize company X'."
        ),
    }


def route_with_llm_intent(question: str, db_path=None) -> Optional[Dict[str, Any]]:
    """Route unknown questions via LM Studio intent classifier."""
    kwargs = {}
    if db_path is not None:
        kwargs["db_path"] = db_path

    # Get LLM classification
    prompt = _get_intent_classification_prompt(question)
    raw = call_lmstudio_for_text(prompt, timeout_s=8.0)

    if not raw:
        return None

    parsed = parse_llm_intent_payload(raw)

    # Validate intent before executing
    if parsed.intent == "unknown":
        return None

    return _execute_intents_from_dict(parsed, **kwargs)


def _get_intent_classification_prompt(question: str) -> str:
    """Build prompt for LM Studio intent classifier."""
    return f"""You classify user questions for a SQLite leads database.

Return JSON only. No markdown. No prose. No SQL.

Allowed intents:
- count_leads
- top_leads
- leads_without_contacts
- followups_due
- summarize_company
- unknown

JSON schema:
{{
  "intent": "top_leads",
  "company": null,
  "limit": 10,
  "filters": {{}},
  "confidence": 0.0
}}

User question:
{question!r}""".strip()


def resolve_ask_database_intent(question: str, *, use_lmstudio: bool = True) -> AskDatabaseIntent:
    """Resolve question to an intent using deterministic or LM Studio routing."""
    deterministic = deterministic_ask_intent(question)
    if deterministic is not None:
        return deterministic

    if use_lmstudio:
        # Directly get LLM classification (route_with_llm_intent already uses call_lmstudio_for_text)
        prompt = _get_intent_classification_prompt(question)
        raw = call_lmstudio_for_text(prompt, timeout_s=8.0)
        if raw:
            return parse_llm_intent_payload(raw)

    return AskDatabaseIntent(intent="unknown")


def _execute_intents_from_dict(intent: AskDatabaseIntent, **kwargs) -> Dict[str, Any]:
    """Execute intent from parsed dict."""
    if intent.intent == "count_leads":
        count = db.count_potential_clients(**kwargs)
        return {
            "intent": "count_leads",
            "data": {"count": count},
            "answer": f"Imamo {count} potencijalnih klijenata u bazi.",
        }

    if intent.intent == "top_leads":
        leads = db.get_top_leads(int(intent.limit), **kwargs)
        return {
            "intent": "top_leads",
            "data": {"leads": leads},
            "answer": _format_leads_list(leads, f"Top leads by fit score: (limit={int(intent.limit)})"),
        }

    if intent.intent == "leads_without_contacts":
        leads = db.get_leads_without_contacts(**kwargs)
        return {
            "intent": "leads_without_contacts",
            "data": {"leads": leads},
            "answer": _format_leads_list(leads, "Leads without contacts:"),
        }

    if intent.intent == "followups_due":
        items = db.get_followups_due(**kwargs)
        lines = ["Follow-ups due:"]
        for it in items[:10]:  # Limit to 10 for display
            lines.append(
                f"- {it.get('company_name')}: {it.get('title') or it.get('subject')} "
                f"(due {it.get('due_date')})"
            )
        if not lines[1:]:
            answer = "Nema follow-up zadataka ili rokova koji su dospjeli."
        else:
            answer = "\n".join(lines)
        return {
            "intent": "followups_due",
            "data": {"items": items},
            "answer": answer,
        }

    if intent.intent == "summarize_company":
        if not intent.company:
            return _answer_unknown()

        name = db.search_lead_by_name(intent.company, **kwargs)
        if not name:
            return {
                "intent": "summarize_company",
                "data": {},
                "answer": f"Nema leada za '{intent.company}'.",
            }

        detail = db.get_lead(name[0]["id"], **kwargs)
        summary = _summarize_lead(detail)
        return {
            "intent": "summarize_company",
            "data": {"lead": detail},
            "answer": summary,
        }

    return _answer_unknown()


def _format_leads_list(leads: List[Dict[str, Any]], header: str) -> str:
    if not leads:
        return f"{header}\n(none)"
    lines = [header]
    for l in leads[:20]:
        name = l.get("company_name") or "?"
        score = l.get("fit_score", 0)
        status = l.get("status", "")
        lines.append(f"- {name} (fit={score}, status={status})")
    return "\n".join(lines)


def _summarize_lead(lead: Optional[Dict[str, Any]]) -> str:
    if not lead:
        return "Lead not found."
    people = lead.get("people") or []
    dms = [p["name"] for p in people if p.get("is_decision_maker")]
    services = lead.get("services") or []
    parts = [
        f"**{lead.get('company_name')}**",
        f"Website: {lead.get('website') or 'n/a'}",
        f"Partner tier: {lead.get('partner_tier') or 'n/a'}",
        f"Fit score: {lead.get('fit_score', 0)}",
        f"Status: {lead.get('status')}",
        f"Services: {', '.join(services) if services else 'n/a'}",
        f"People: {len(people)} ({len(dms)} decision-makers)",
        f"Interactions: {len(lead.get('interactions') or [])}",
        f"Open tasks: {len([t for t in (lead.get('tasks') or []) if t.get('status') == 'open'])}",
    ]
    if lead.get("description"):
        parts.append(f"Description: {lead['description'][:300]}")
    if dms:
        parts.append(f"Decision makers: {', '.join(dms)}")
    return "\n".join(parts)


def _answer_unknown() -> Dict[str, Any]:
    """Return unknown intent response."""
    count = db.count_potential_clients()
    return {
        "intent": "unknown",
        "data": {"count": count},
        "answer": (
            f"Nisam siguran na pitanje. Trenutno imamo {count} potencijalnih klijenata. "
            "Probaj: 'show top leads', 'show leads without contacts', "
            "'show follow-ups due', 'summarize company X'."
        ),
    }


def answer_question(question: str, use_llm: bool = False, db_path=None) -> Dict[str, Any]:
    """Answer from SQLite; optionally polish with LLM (never as memory)."""
    result = route_question(question, db_path=db_path)
    answer = result["answer"]

    if use_llm and result.get("data"):
        context = f"SQLite query result (source of truth):\n{answer}\n\nData:\n{result['data']}"
        polished = chat_completion(
            [
                {
                    "role": "system",
                    "content": (
                        "You summarize database query results. Use ONLY the provided data. "
                        "Do not invent leads or counts."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Question: {question}\n\n{context}",
                },
            ]
        )
        if polished:
            answer = polished

    return {
        "question": question,
        "intent": result["intent"],
        "answer": answer,
        "data": result.get("data"),
    }
