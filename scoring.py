"""Fit score computation for leads."""

from __future__ import annotations

import re
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
    if not title:
        return {
            "role_type": "other",
            "is_decision_maker": False,
            "is_relevant_contact": False,
            "relevance_reason": None,
        }
    t = " ".join(title.casefold().replace("e-commerce", "ecommerce").split())
    if re.search(r"\b(?:assistant|executive assistant)\b", t):
        return {
            "role_type": "other",
            "is_decision_maker": False,
            "is_relevant_contact": False,
            "relevance_reason": None,
        }
    economic = (
        r"\b(?:co[- ]?founder|founder|ceo|chief executive officer|president)\b",
        r"(?:^|\b(?:business|agency|company|store)\s+)owner\b",
        r"\bmanaging partner\b",
    )
    senior_operational = (
        r"\b(?:coo|chief operating officer)\b",
        r"\b(?:head|director|vp|vice president)\s+(?:of\s+)?(?:ecommerce|shopify|operations)\b",
        r"\b(?:ecommerce|shopify|operations)\s+director\b",
    )
    operational = senior_operational + (
        r"\b(?:ecommerce|shopify|operations)\s+(?:manager|lead|specialist|coordinator)\b",
    )
    technical = (
        r"\b(?:cto|chief technology officer|solutions architect|technical director|engineering director)\b",
        r"\b(?:head|director|vp|vice president)\s+(?:of\s+)?(?:engineering|technology)\b",
    )
    workflow = (
        r"\b(?:project|delivery|account|client success|customer success|partnerships)\s+manager\b",
    )

    def matches(patterns: tuple[str, ...]) -> bool:
        return any(re.search(pattern, t) for pattern in patterns)

    if matches(economic):
        role_type = "economic_buyer"
    elif matches(operational):
        role_type = "operational_owner"
    elif matches(technical):
        role_type = "technical_influencer"
    elif matches(workflow):
        role_type = "workflow_user"
    else:
        role_type = "other"
    is_dm = role_type == "economic_buyer" or (
        role_type == "operational_owner" and matches(senior_operational)
    )
    is_rel = role_type != "other"
    reason = f"title classified as {role_type}" if is_rel else None
    return {
        "role_type": role_type,
        "is_decision_maker": is_dm,
        "is_relevant_contact": is_rel,
        "relevance_reason": reason,
    }


def derive_person_role_fields(
    title: Optional[str],
    role_type: Optional[str] = None,
) -> Dict[str, Any]:
    """Derive all legacy role fields from one controlled classification."""
    from models import ROLE_TYPES

    classification = classify_person_title(title)
    if role_type is None:
        return classification
    if role_type not in ROLE_TYPES:
        raise ValueError(f"Unsupported role_type: {role_type}")
    is_decision_maker = role_type == "economic_buyer" or (
        role_type == "operational_owner" and classification["is_decision_maker"]
    )
    is_relevant_contact = role_type != "other"
    return {
        "role_type": role_type,
        "is_decision_maker": is_decision_maker,
        "is_relevant_contact": is_relevant_contact,
        "relevance_reason": (
            f"title classified as {role_type}" if is_relevant_contact else None
        ),
    }
