"""Data model constants for the lead intelligence app."""

from __future__ import annotations

import math
from typing import Any

SOURCE_TYPES = (
    "shopify_directory",
    "linkedin_company",
    "linkedin_person",
    "website",
    "email",
    "note",
)

LEAD_STATUSES = ("new", "researching", "qualified", "contacted", "active", "closed", "archived")

EXTRACTION_STATUSES = ("ok", "needs_review", "fallback")

ROLE_TYPES = (
    "economic_buyer",
    "operational_owner",
    "technical_influencer",
    "workflow_user",
    "other",
)

EMAIL_STATUSES = (
    "published",
    "verified",
    "pattern_derived",
    "inferred",
    "unknown",
)

ENRICHMENT_STATUSES = (
    "pending",
    "in_progress",
    "people_found",
    "email_pending",
    "ready",
    "needs_review",
    "no_result",
)


def normalize_email_enrichment(
    *,
    has_email: bool,
    email_status: Any = None,
    email_confidence: Any = None,
    last_verified_at: Any = None,
) -> dict[str, Any]:
    """Validate and fail closed for legacy person-email enrichment metadata."""
    if not has_email:
        return {
            "email_status": "unknown",
            "email_confidence": 0.0,
            "last_verified_at": None,
        }

    status = email_status or "unknown"
    if status not in EMAIL_STATUSES:
        raise ValueError(f"Unsupported email_status: {status}")
    try:
        confidence = float(email_confidence or 0.0)
    except (TypeError, ValueError) as exc:
        raise ValueError("email_confidence must be between 0 and 1") from exc
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise ValueError("email_confidence must be between 0 and 1")

    # A verified claim without verification evidence is not verified.
    if status == "verified" and not last_verified_at:
        status = "unknown"
    if status != "verified":
        last_verified_at = None
    return {
        "email_status": status,
        "email_confidence": confidence,
        "last_verified_at": last_verified_at,
    }
