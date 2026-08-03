"""Validation and controlled values for noncanonical enrichment candidates."""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlparse

from models import ROLE_TYPES

CANDIDATE_STATUSES = ("needs_review", "conflict", "approved", "applied", "rejected", "stale")
EVIDENCE_BASES = ("source_confirmed", "inferred", "operator_entered")
DISCOVERY_METHODS = ("manual_paste", "deterministic_parser", "local_llm_extraction")
CONTACT_KINDS = ("email", "phone", "linkedin", "website", "other")
VERIFICATION_STATUSES = ("unverified", "syntax_valid", "source_confirmed", "verified", "rejected", "stale")


class CandidateError(ValueError):
    """Stable domain error for candidate persistence."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def require_enum(value: str, allowed: tuple[str, ...], code: str, label: str) -> str:
    if value not in allowed:
        raise CandidateError(code, f"Unsupported {label}: {value}")
    return value


def require_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError) as exc:
        raise CandidateError("invalid_confidence", "confidence must be between 0 and 1") from exc
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise CandidateError("invalid_confidence", "confidence must be between 0 and 1")
    return confidence


def normalize_candidate_name(value: str) -> str:
    from db import normalize_name

    if not value or not value.strip():
        raise CandidateError("invalid_candidate_name", "candidate name cannot be blank")
    normalized = normalize_name(value)
    if not normalized:
        raise CandidateError("invalid_candidate_name", "normalized candidate name cannot be blank")
    return normalized


def normalize_profile_url(value: Optional[str]) -> Optional[str]:
    if value is None or not value.strip():
        return None
    parsed = urlparse(value.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
        raise CandidateError("invalid_profile_url", "profile_url must be a safe HTTP(S) URL")
    return value.strip().rstrip("/").lower()


def normalize_contact_value(kind: str, value: str) -> str:
    if not value or not value.strip():
        raise CandidateError("invalid_contact_value", "contact value cannot be blank")
    text = value.strip()
    if kind == "email":
        from db import normalize_email

        normalized = normalize_email(text)
        if not normalized:
            raise CandidateError("invalid_contact_value", "email value is invalid")
        return normalized
    if kind == "phone":
        return re.sub(r"\D", "", text) or text.casefold()
    if kind in ("linkedin", "website"):
        return normalize_profile_url(text) or ""
    return " ".join(text.split()).casefold()


def validate_verification(evidence_basis: str, verification_status: str, verified_at: Optional[str]) -> None:
    require_enum(evidence_basis, EVIDENCE_BASES, "invalid_evidence_basis", "evidence_basis")
    require_enum(verification_status, VERIFICATION_STATUSES, "invalid_verification_state", "verification_status")
    if evidence_basis == "inferred" and verification_status in ("source_confirmed", "verified"):
        raise CandidateError("invalid_verification_state", "inferred evidence cannot be source-confirmed or verified")
    if verification_status == "verified":
        if not verified_at:
            raise CandidateError("invalid_verification_state", "verified candidates require verified_at")
        try:
            datetime.fromisoformat(verified_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CandidateError("invalid_verification_state", "verified_at must be an ISO timestamp") from exc
    elif verified_at is not None:
        raise CandidateError("invalid_verification_state", "verified_at must be null unless verified")
