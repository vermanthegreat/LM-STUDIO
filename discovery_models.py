"""Provider-neutral, bounded contracts for future contact discovery."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import parse_qsl, urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from candidate_models import (
    CONTACT_KINDS,
    DISCOVERY_METHODS,
    EVIDENCE_BASES,
    VERIFICATION_STATUSES,
    normalize_candidate_name,
    normalize_contact_value,
)
from models import ROLE_TYPES, SOURCE_TYPES


MAX_TEXT = 20_000
MAX_NAME = 200
MAX_IDENTIFIER = 128
MAX_REASON = 500
MAX_WARNING = 120
MAX_AMBIGUITY_CODE = 64
SECRET_KEY_PARTS = (
    "api_key", "token", "access_token", "refresh_token", "password", "secret",
    "cookie", "authorization", "session",
)
SAFE_URL_SCHEMES = {"http", "https"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
SECRET_VALUE_RE = re.compile(
    r"(?:api[_-]?key|apikey|token|access[_-]?token|refresh[_-]?token|password|secret|cookie|authorization|session)\s*[:=]\s*\S+|(?:bearer|basic)\s+\S+",
    re.IGNORECASE,
)
SECRET_QUERY_KEY_RE = re.compile(
    r"(?:^|_)(?:api_?key|token|access_token|refresh_token|password|secret|cookie|authorization|session)(?:_|$)",
    re.IGNORECASE,
)


class DiscoveryContractError(ValueError):
    """Stable validation error for discovery contract boundaries."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _reject_secret_key(key: str) -> None:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
    if any(
        normalized == part
        or re.search(rf"(?:^|_){re.escape(part)}(?:_|$)", normalized)
        for part in SECRET_KEY_PARTS
    ):
        raise ValueError("secret-bearing fields are not permitted")


def _url_contains_secret(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme not in SAFE_URL_SCHEMES:
        return False
    if parsed.username or parsed.password:
        return True
    for component in (parsed.query, parsed.fragment):
        if any(SECRET_QUERY_KEY_RE.search(key) for key, _ in parse_qsl(component, keep_blank_values=True)):
            return True
    return False


def reject_secrets(value: Any) -> Any:
    """Recursively reject credential-shaped keys and values without echoing them."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            _reject_secret_key(str(key))
            reject_secrets(child)
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            reject_secrets(child)
    elif isinstance(value, str):
        if _url_contains_secret(value):
            raise ValueError("credential-bearing URLs are not permitted")
        if SECRET_VALUE_RE.search(value):
            raise ValueError("credential-bearing values are not permitted")
    return value


def _safe_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in SAFE_URL_SCHEMES or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("URL must be a safe HTTP(S) URL")
    return value.rstrip("/")


def _safe_domain(value: str) -> str:
    normalized = value.strip().casefold().rstrip(".")
    if not DOMAIN_RE.fullmatch(normalized) or "/" in normalized or "@" in normalized:
        raise ValueError("domain must be a normalized hostname")
    return normalized


def _bounded_text(value: str, *, limit: int, label: str) -> str:
    cleaned = value.strip()
    if len(cleaned) > limit:
        raise ValueError(f"{label} exceeds its bounded length")
    return cleaned


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @model_validator(mode="before")
    @classmethod
    def reject_credential_data(cls, value: Any) -> Any:
        reject_secrets(value)
        return value


class DiscoveryOutcomeStatus(str, Enum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    NO_RESULT = "no_result"
    NEEDS_REVIEW = "needs_review"
    RATE_LIMITED = "rate_limited"
    RETRYABLE_ERROR = "retryable_error"
    PERMANENT_ERROR = "permanent_error"
    CANCELLED = "cancelled"


class DiscoveryRequest(ContractModel):
    job_id: str = Field(min_length=1, max_length=MAX_IDENTIFIER)
    lead_id: int = Field(gt=0)
    company_name: str = Field(min_length=1, max_length=MAX_NAME)
    normalized_domain: str = Field(min_length=1, max_length=253)
    company_website: str = Field(min_length=1, max_length=2048)
    target_roles: list[str] = Field(min_length=1, max_length=3)
    result_limit: int = Field(ge=1, le=5)
    approved_source_types: list[str] = Field(min_length=1)
    requested_at: datetime
    requester_identity: str = Field(min_length=1, max_length=MAX_IDENTIFIER)
    correlation_id: str = Field(min_length=1, max_length=MAX_IDENTIFIER)
    provider_config_ref: str = Field(min_length=1, max_length=MAX_IDENTIFIER)
    max_pages: int = Field(ge=1, le=5)
    max_requests: int = Field(ge=1, le=5)
    timeout_seconds: int = Field(ge=1, le=30)

    @field_validator("company_name", "job_id", "requester_identity", "correlation_id", "provider_config_ref")
    @classmethod
    def bounded_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @field_validator("normalized_domain")
    @classmethod
    def normalize_domain(cls, value: str) -> str:
        return _safe_domain(value)

    @field_validator("company_website")
    @classmethod
    def validate_website(cls, value: str) -> str:
        return _safe_url(value)

    @field_validator("target_roles")
    @classmethod
    def validate_roles(cls, value: Iterable[str]) -> list[str]:
        roles = list(dict.fromkeys(value))
        if not roles or any(role not in ROLE_TYPES for role in roles):
            raise ValueError("target_roles must contain only controlled roles")
        return roles

    @field_validator("approved_source_types")
    @classmethod
    def validate_source_types(cls, value: Iterable[str]) -> list[str]:
        source_types = list(dict.fromkeys(value))
        if not source_types or any(source_type not in SOURCE_TYPES for source_type in source_types):
            raise ValueError("approved_source_types must contain only controlled source types")
        return source_types

    @field_validator("requested_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("requested_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class DiscoverySource(ContractModel):
    source_url: str = Field(min_length=1, max_length=2048)
    canonical_url: str = Field(min_length=1, max_length=2048)
    source_type: str
    page_title: str = Field(default="", max_length=MAX_NAME)
    retrieved_at: datetime
    content_hash: str
    extracted_text: str = Field(default="", max_length=MAX_TEXT)
    http_status: Optional[int] = Field(default=None, ge=100, le=599)
    content_type: Optional[str] = Field(default=None, max_length=128)
    provider_request_id: str = Field(min_length=1, max_length=MAX_IDENTIFIER)
    warning_codes: list[str] = Field(default_factory=list, max_length=20)

    _source_url = field_validator("source_url", "canonical_url")(_safe_url)

    @field_validator("source_type")
    @classmethod
    def validate_source_type(cls, value: str) -> str:
        if value not in SOURCE_TYPES:
            raise ValueError("unsupported source type")
        return value

    @field_validator("content_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        if not SHA256_RE.fullmatch(value.casefold()):
            raise ValueError("content_hash must be a SHA-256 hex digest")
        return value.casefold()

    @field_validator("retrieved_at")
    @classmethod
    def source_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("retrieved_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class DiscoveryContact(ContractModel):
    kind: str
    value: str = Field(min_length=1, max_length=2048)
    normalized_value: str = Field(min_length=1, max_length=2048)
    evidence_basis: str
    verification_status: str
    source_url: str = Field(min_length=1, max_length=2048)

    @field_validator("kind")
    @classmethod
    def validate_kind(cls, value: str) -> str:
        if value not in CONTACT_KINDS:
            raise ValueError("unsupported contact kind")
        return value

    @field_validator("evidence_basis")
    @classmethod
    def validate_evidence(cls, value: str) -> str:
        if value not in EVIDENCE_BASES:
            raise ValueError("unsupported evidence basis")
        return value

    @field_validator("verification_status")
    @classmethod
    def validate_verification(cls, value: str) -> str:
        if value not in VERIFICATION_STATUSES or value == "verified":
            raise ValueError("discovery output cannot claim external verification")
        return value

    @model_validator(mode="after")
    def validate_truthfulness(self) -> "DiscoveryContact":
        if self.evidence_basis == "inferred" and self.verification_status == "source_confirmed":
            raise ValueError("inferred contact cannot be source-confirmed")
        _safe_url(self.source_url)
        try:
            expected = normalize_contact_value(self.kind, self.value)
        except ValueError as exc:
            raise ValueError("contact value cannot be normalized") from exc
        if self.normalized_value != expected:
            raise ValueError("normalized contact value is inconsistent with value")
        return self


class DiscoveryCandidate(ContractModel):
    name: str = Field(min_length=1, max_length=MAX_NAME)
    normalized_name: str = Field(min_length=1, max_length=MAX_NAME)
    title: Optional[str] = Field(default=None, max_length=MAX_NAME)
    role_type: str
    is_decision_maker: bool = False
    profile_url: Optional[str] = Field(default=None, max_length=2048)
    explicit_contacts: list[DiscoveryContact] = Field(default_factory=list, max_length=10)
    source_url: str = Field(min_length=1, max_length=2048)
    source_type: str
    confidence: float = Field(ge=0, le=1)
    relevance_reason: Optional[str] = Field(default=None, max_length=MAX_REASON)
    evidence_basis: str
    discovery_method: str = Field(min_length=1, max_length=64)
    raw_evidence_reference: Optional[str] = Field(default=None, max_length=MAX_IDENTIFIER)
    ambiguity_conflict_signals: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("role_type")
    @classmethod
    def validate_role(cls, value: str) -> str:
        if value not in ROLE_TYPES:
            raise ValueError("unsupported role type")
        return value

    @field_validator("source_type")
    @classmethod
    def validate_source(cls, value: str) -> str:
        if value not in SOURCE_TYPES:
            raise ValueError("unsupported source type")
        return value

    @field_validator("evidence_basis")
    @classmethod
    def validate_basis(cls, value: str) -> str:
        if value not in EVIDENCE_BASES:
            raise ValueError("unsupported evidence basis")
        return value

    @field_validator("discovery_method")
    @classmethod
    def validate_discovery_method(cls, value: str) -> str:
        if value not in DISCOVERY_METHODS:
            raise ValueError("unsupported discovery method")
        return value

    @field_validator("profile_url", "source_url")
    @classmethod
    def validate_candidate_url(cls, value: Optional[str]) -> Optional[str]:
        return _safe_url(value) if value else value

    @model_validator(mode="after")
    def validate_name_normalization(self) -> "DiscoveryCandidate":
        try:
            expected = normalize_candidate_name(self.name)
        except ValueError as exc:
            raise ValueError("candidate name cannot be normalized") from exc
        if self.normalized_name != expected:
            raise ValueError("normalized candidate name is inconsistent with name")
        return self

    @field_validator("ambiguity_conflict_signals")
    @classmethod
    def validate_signals(cls, value: Iterable[str]) -> list[str]:
        signals = list(dict.fromkeys(value))
        if any(not re.fullmatch(r"[a-z0-9_:-]{1,64}", signal) for signal in signals):
            raise ValueError("ambiguity signals must be controlled codes")
        return signals


class DiscoveryOutcome(ContractModel):
    provider_name: str = Field(min_length=1, max_length=MAX_IDENTIFIER)
    provider_request_id: str = Field(min_length=1, max_length=MAX_IDENTIFIER)
    status: DiscoveryOutcomeStatus
    started_at: datetime
    completed_at: datetime
    sources: list[DiscoverySource] = Field(default_factory=list, max_length=5)
    candidates: list[DiscoveryCandidate] = Field(default_factory=list, max_length=5)
    warnings: list[str] = Field(default_factory=list, max_length=20)
    retry_after: Optional[datetime] = None
    no_result_reason: Optional[str] = Field(default=None, max_length=MAX_REASON)
    safe_error_code: Optional[str] = Field(default=None, max_length=MAX_IDENTIFIER)

    @model_validator(mode="after")
    def validate_outcome(self) -> "DiscoveryOutcome":
        if self.completed_at < self.started_at:
            raise ValueError("completed_at cannot precede started_at")
        if self.status == DiscoveryOutcomeStatus.SUCCEEDED and not self.candidates:
            raise ValueError("succeeded outcome requires a candidate")
        if self.status == DiscoveryOutcomeStatus.NO_RESULT and (self.candidates or not self.no_result_reason):
            raise ValueError("no_result requires a reason and no candidates")
        if self.status == DiscoveryOutcomeStatus.RATE_LIMITED and self.retry_after is None:
            raise ValueError("rate_limited outcome requires retry_after")
        if self.status == DiscoveryOutcomeStatus.PERMANENT_ERROR and self.retry_after is not None:
            raise ValueError("permanent_error cannot carry retry metadata")
        if self.status == DiscoveryOutcomeStatus.CANCELLED and self.candidates:
            raise ValueError("cancelled outcome cannot contain candidates")
        return self

    def validate_for_request(self, request: DiscoveryRequest) -> "DiscoveryOutcome":
        if len(self.candidates) > request.result_limit:
            raise ValueError("candidate count exceeds request result_limit")
        if len(self.sources) > min(request.max_pages, request.max_requests):
            raise ValueError("source count exceeds request bounds")
        return self


def content_hash(text: str) -> str:
    """Return the contract's stable content hash representation."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
