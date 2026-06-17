"""Fit score computation for leads."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


PLUS_SIGNALS = ("shopify plus", "plus partner", "plus certified", "plus agency")
SERVICE_SIGNALS = (
    "website management",
    "store setup",
    "store build",
    "migration",
    "cro",
    "conversion",
    "catalog",
    "product ops",
    "ongoing support",
    "managed services",
    "shopify development",
    "ecommerce development",
    "e-commerce",
)
AGENCY_SIGNALS = ("agency", "partner", "studio", "consulting", "solutions", "services")
ECOMMERCE_SIGNALS = ("shopify", "ecommerce", "e-commerce", "dtc", "direct to consumer")
GOVERNANCE_SIGNALS = (
    "catalog",
    "pim",
    "governance",
    "compliance",
    "manual review",
    "product data",
    "merchandising",
    "ops",
)


def _text_blob(lead: Dict[str, Any]) -> str:
    parts = [
        lead.get("company_name") or "",
        lead.get("description") or "",
        lead.get("partner_tier") or "",
        " ".join(lead.get("services") or []),
        " ".join(lead.get("industries") or []),
    ]
    return " ".join(parts).lower()


def compute_fit_score(
    lead: Dict[str, Any],
    people: Optional[List[Dict[str, Any]]] = None,
) -> int:
    """Return fit_score 0-100."""
    score = 0
    blob = _text_blob(lead)
    people = people or []

    if any(s in blob for s in PLUS_SIGNALS):
        score += 20
    elif "plus" in blob:
        score += 10

    service_hits = sum(1 for s in SERVICE_SIGNALS if s in blob)
    score += min(25, service_hits * 5)

    if any(s in blob for s in AGENCY_SIGNALS):
        score += 15

    if lead.get("website") or lead.get("domain"):
        score += 10

    if any(p.get("is_decision_maker") for p in people):
        score += 15
    elif any(p.get("is_relevant_contact") for p in people):
        score += 8

    ecommerce_hits = sum(1 for s in ECOMMERCE_SIGNALS if s in blob)
    score += min(15, ecommerce_hits * 5)

    gov_hits = sum(1 for s in GOVERNANCE_SIGNALS if s in blob)
    score += min(10, gov_hits * 3)

    tier = (lead.get("partner_tier") or "").lower()
    if "plus" in tier:
        score += 5
    if "premier" in tier or "platinum" in tier:
        score += 5

    return max(0, min(100, score))


def classify_person_title(title: Optional[str]) -> Dict[str, Any]:
    from models import DECISION_MAKER_TITLES, RELEVANT_TITLE_KEYWORDS

    if not title:
        return {"is_decision_maker": False, "is_relevant_contact": False, "relevance_reason": None}
    t = title.lower().strip()
    is_dm = any(kw in t for kw in DECISION_MAKER_TITLES)
    is_rel = is_dm or any(kw in t for kw in RELEVANT_TITLE_KEYWORDS)
    reason = None
    if is_dm:
        reason = "title matches decision-maker pattern"
    elif is_rel:
        reason = "title matches relevant contact pattern"
    return {
        "is_decision_maker": is_dm,
        "is_relevant_contact": is_rel,
        "relevance_reason": reason,
    }
