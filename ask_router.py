"""Ask Database router for the FastAPI web app.

Flow:
1. Receive a free-form user question.
2. Use LM Studio, when enabled, only to decode the request into a safe intent.
3. Execute read-only SQLite queries against leads.db.
4. Return database-grounded results; optional LLM polish never invents data.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import db
from llm import chat_completion, call_lmstudio_for_text


@dataclass(frozen=True)
class AskIntent:
    name: str
    query: str = ""
    company: str = ""
    limit: int = 10
    filters: Dict[str, Any] | None = None
    confidence: float = 0.0


_ALLOWED_INTENTS = {
    "count_leads",
    "top_leads",
    "leads_without_contacts",
    "followups_due",
    "summarize_company",
    "search_leads",
    "unknown",
}

_STOPWORDS = {
    "and", "are", "for", "from", "that", "the", "with", "who", "which", "what",
    "find", "show", "list", "give", "lead", "leads", "agency", "agencies",
    "company", "companies", "koje", "koja", "koji", "rade", "radi", "agencije",
    "agencija", "kompanije", "klijenti", "daj", "nadji", "prikazi", "imamo", "ima",
    "bazi", "preko", "iznad",
}


def answer_question(question: str, use_llm: bool = False, db_path=None) -> Dict[str, Any]:
    """Answer a free-form Ask Database question from SQLite."""
    q = (question or "").strip()
    if not q:
        return {"question": q, "intent": "empty", "answer": "Unesi pitanje.", "data": None}

    kwargs = {"db_path": db_path} if db_path is not None else {}
    intent = _deterministic_intent(q)

    if intent.name == "unknown" and use_llm:
        intent = _llm_intent(q)

    if intent.name == "unknown":
        intent = _fallback_search_intent(q)

    result = _execute_intent(intent, **kwargs)
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
        return AskIntent("count_leads", confidence=1.0)

    if any(term in q for term in ("top leads", "best leads", "najbolj", "top klijenti")):
        return AskIntent("top_leads", limit=_extract_limit(q), confidence=1.0)

    if "without contact" in q or "bez kontakt" in q:
        return AskIntent("leads_without_contacts", confidence=1.0)

    if "follow" in q or "deadline" in q or "rok" in q:
        return AskIntent("followups_due", confidence=1.0)

    for prefix in ("summarize company ", "summary company ", "rezimiraj kompaniju ", "sumiraj kompaniju "):
        if q.startswith(prefix):
            return AskIntent("summarize_company", company=question[len(prefix):].strip(), confidence=1.0)

    if any(term in q for term in ("product description", "opis proizvoda", "opisi proizvoda", "seo", "copywriting", "content")):
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
- followups_due
- summarize_company
- search_leads
- unknown

Use search_leads for free-form filtering by service, location, industry, partner tier, score, status, company text, description, or contact availability.

Available lead fields: company_name, website, partner_tier, rating, review_count, primary_location, services, locations, industries, description, fit_score, status, people_count, has_decision_maker.

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


def _execute_intent(intent: AskIntent, **kwargs) -> Dict[str, Any]:
    if intent.name == "count_leads":
        count = db.count_potential_clients(**kwargs)
        return {"intent": intent.name, "answer": f"Imamo {count} potencijalnih klijenata u bazi.", "data": {"count": count}}

    if intent.name == "top_leads":
        leads = db.get_top_leads(intent.limit, **kwargs)
        return {"intent": intent.name, "answer": _format_leads(leads, "Top leads:"), "data": {"leads": leads}}

    if intent.name == "leads_without_contacts":
        leads = db.get_leads_without_contacts(**kwargs)
        return {"intent": intent.name, "answer": _format_leads(leads, "Leads without contacts:"), "data": {"leads": leads}}

    if intent.name == "followups_due":
        items = db.get_followups_due(**kwargs)
        lines = ["Follow-ups due:"]
        for item in items[:10]:
            lines.append(f"- {item.get('company_name')}: {item.get('title') or item.get('subject')} (due {item.get('due_date')})")
        if len(lines) == 1:
            lines.append("(none)")
        return {"intent": intent.name, "answer": "\n".join(lines), "data": {"items": items}}

    if intent.name == "summarize_company":
        if not intent.company:
            return _unknown(**kwargs)
        rows = db.search_lead_by_name(intent.company, **kwargs)
        if not rows:
            return {"intent": intent.name, "answer": f"Nema leada za '{intent.company}'.", "data": {}}
        lead = db.get_lead(rows[0]["id"], **kwargs)
        return {"intent": intent.name, "answer": _summarize_lead(lead), "data": {"lead": lead}}

    if intent.name == "search_leads":
        leads = db.list_leads(**kwargs)
        matches = _search_leads(leads, intent)
        return {
            "intent": intent.name,
            "answer": _format_search(matches, intent),
            "data": {"leads": matches, "parsed_query": intent.query, "filters": intent.filters or {}, "confidence": intent.confidence},
        }

    return _unknown(**kwargs)


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


def _format_leads(leads: List[Dict[str, Any]], header: str) -> str:
    if not leads:
        return f"{header}\n(none)"
    lines = [header]
    for lead in leads[:20]:
        lines.append(f"- {lead.get('company_name') or '?'} (fit={lead.get('fit_score', 0)}, status={lead.get('status')})")
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
            f"services={service_text}, website={lead.get('website') or 'n/a'})"
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


def _unknown(**kwargs) -> Dict[str, Any]:
    count = db.count_potential_clients(**kwargs)
    return {
        "intent": "unknown",
        "answer": f"Nisam siguran na pitanje. Trenutno imamo {count} potencijalnih klijenata.",
        "data": {"count": count},
    }


def _polish_answer(question: str, answer: str, data: Dict[str, Any]) -> Optional[str]:
    return chat_completion(
        [
            {"role": "system", "content": "Summarize database results. Use only provided data. Do not invent."},
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
    for key in ("company_name", "website", "partner_tier", "primary_location", "description", "status"):
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


def _extract_limit(text: str) -> int:
    match = re.search(r"\b(\d{1,2})\b", text)
    return _clamp_limit(match.group(1) if match else None)


def _clamp_limit(value: Any, default: int = 10, maximum: int = 25) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, maximum))


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text).lower()).strip()
