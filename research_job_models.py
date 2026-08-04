"""Typed, durable models for bounded contact-research jobs."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from discovery_models import DiscoveryRequest, reject_secrets


ADAPTER_KEYS = ("fake", "company_website")
RESEARCH_JOB_ACTIVE_STATES = ("queued", "claimed", "running", "retry_wait")
RESEARCH_JOB_TERMINAL_STATES = (
    "succeeded", "partial", "no_result", "needs_review", "failed", "cancelled", "abandoned",
)
MAX_PRIORITY = 100
MAX_SNAPSHOT_LENGTH = 20_000
MIN_LEASE_SECONDS = 30
DEFAULT_LEASE_SECONDS = 120
MAX_LEASE_SECONDS = 300
MAX_WORKER_ID_LENGTH = 128
MAX_RESULT_WARNING_CODES = 20
MAX_RESULT_NOTE_LENGTH = 240
MAX_RETRY_DELAY_SECONDS = 24 * 60 * 60
MAX_RECOVERY_LIMIT = 100
SAFE_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_:-]{0,63}$")


class ResearchJobError(ValueError):
    """Stable domain error for research-job persistence boundaries."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ResearchJobStatus(str, Enum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    NO_RESULT = "no_result"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


class ResearchJobTerminalStatus(str, Enum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    NO_RESULT = "no_result"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


def _safe_code(value: str, label: str) -> str:
    cleaned = value.strip().casefold()
    if not SAFE_CODE_RE.fullmatch(cleaned):
        raise ValueError(f"{label} must be a safe controlled code")
    reject_secrets(cleaned)
    return cleaned


def _utc_datetime(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


class _RedactedValidationModel(BaseModel):
    """Lifecycle-only Pydantic boundary that never returns rejected input values."""

    @classmethod
    def _redacted_errors(cls, error: ValidationError) -> list[dict[str, object]]:
        redacted: list[dict[str, object]] = []
        for item in error.errors():
            clean = {key: value for key, value in item.items() if key not in {"input", "ctx"}}
            error_type = clean.get("type")
            if error_type in {"value_error", "assertion_error"}:
                clean["ctx"] = {"error": ValueError(str(clean.get("msg", "validation failed")))}
            elif item.get("ctx") is not None:
                clean["ctx"] = item["ctx"]
            redacted.append(clean)
        return redacted

    def __init__(self, **data: object) -> None:
        try:
            super().__init__(**data)
        except ValidationError as error:
            raise ValidationError.from_exception_data(self.__class__.__name__, self._redacted_errors(error)) from None


def _optional_bounded_code(value: Optional[str]) -> Optional[str]:
    if value is None or not isinstance(value, str):
        return None
    normalized = value.strip().casefold()
    if not SAFE_CODE_RE.fullmatch(normalized):
        return None
    try:
        reject_secrets(normalized)
    except ValueError:
        return None
    return normalized


class ResearchJobResultSummary(_RedactedValidationModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True)

    source_count: int = Field(ge=0)
    candidate_count: int = Field(ge=0)
    warning_codes: tuple[str, ...] = Field(default_factory=tuple, max_length=MAX_RESULT_WARNING_CODES)
    reason_code: Optional[str] = Field(default=None, max_length=64)
    note: Optional[str] = Field(default=None, max_length=MAX_RESULT_NOTE_LENGTH)
    underlying_result_code: Optional[str] = Field(default=None, max_length=64)

    @field_validator("warning_codes")
    @classmethod
    def normalize_warning_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted({_safe_code(item, "warning code") for item in value}))
        if len(normalized) > MAX_RESULT_WARNING_CODES:
            raise ValueError("warning codes exceed their bounded count")
        return normalized

    @field_validator("reason_code")
    @classmethod
    def normalize_reason_code(cls, value: Optional[str]) -> Optional[str]:
        return _safe_code(value, "reason code") if value is not None else None

    @field_validator("note")
    @classmethod
    def validate_note(cls, value: Optional[str]) -> Optional[str]:
        if value is not None:
            reject_secrets(value)
        return value

    @field_validator("underlying_result_code")
    @classmethod
    def normalize_underlying_code(cls, value: Optional[str]) -> Optional[str]:
        return _optional_bounded_code(value)

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ResearchJobFinalization(_RedactedValidationModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    status: ResearchJobTerminalStatus
    summary: ResearchJobResultSummary
    safe_error_code: Optional[str] = Field(default=None, max_length=64)

    @field_validator("safe_error_code")
    @classmethod
    def normalize_error_code(cls, value: Optional[str]) -> Optional[str]:
        return _safe_code(value, "error code") if value is not None else None

    @model_validator(mode="after")
    def validate_status_invariants(self) -> "ResearchJobFinalization":
        summary = self.summary
        explained = bool(summary.warning_codes or summary.reason_code)
        if self.status is ResearchJobTerminalStatus.SUCCEEDED and summary.candidate_count < 1:
            raise ValueError("succeeded finalization requires candidates")
        if self.status is ResearchJobTerminalStatus.PARTIAL and (not (summary.source_count or summary.candidate_count) or not explained):
            raise ValueError("partial finalization requires evidence and an explanation")
        if self.status is ResearchJobTerminalStatus.NO_RESULT and (summary.candidate_count != 0 or not summary.reason_code):
            raise ValueError("no_result finalization requires zero candidates and a reason")
        if self.status is ResearchJobTerminalStatus.NEEDS_REVIEW and not explained:
            raise ValueError("needs_review finalization requires an explanation")
        if self.status is ResearchJobTerminalStatus.FAILED and (not self.safe_error_code or summary.candidate_count != 0):
            raise ValueError("failed finalization requires an error code and zero candidates")
        if self.status is not ResearchJobTerminalStatus.FAILED and self.safe_error_code is not None:
            raise ValueError("terminal failure code is only valid for failed jobs")
        return self


class ResearchJobRetrySchedule(_RedactedValidationModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True)

    safe_error_code: str = Field(min_length=1, max_length=64)
    retry_at: datetime
    reason: Optional[str] = Field(default=None, max_length=MAX_RESULT_NOTE_LENGTH)
    underlying_result_code: Optional[str] = Field(default=None, max_length=64)

    @field_validator("safe_error_code")
    @classmethod
    def normalize_retry_code(cls, value: str) -> str:
        return _safe_code(value, "retry error code")

    @field_validator("retry_at")
    @classmethod
    def normalize_retry_at(cls, value: datetime) -> datetime:
        return _utc_datetime(value, "retry time")

    @field_validator("reason")
    @classmethod
    def validate_retry_reason(cls, value: Optional[str]) -> Optional[str]:
        if value is not None:
            reject_secrets(value)
        return value

    @field_validator("underlying_result_code")
    @classmethod
    def normalize_underlying_code(cls, value: Optional[str]) -> Optional[str]:
        return _optional_bounded_code(value)


class ResearchJobCancellation(_RedactedValidationModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True)

    reason_code: Optional[str] = Field(default=None, max_length=64)

    @field_validator("reason_code")
    @classmethod
    def normalize_cancellation_code(cls, value: Optional[str]) -> Optional[str]:
        return _safe_code(value, "cancellation reason") if value is not None else None


class ResearchJobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True)

    request: DiscoveryRequest
    adapter_key: str = Field(min_length=1, max_length=64)
    priority: int = Field(ge=0, le=MAX_PRIORITY)
    max_attempts: int = Field(ge=1, le=3)
    not_before: Optional[datetime] = None

    @field_validator("request", mode="before")
    @classmethod
    def require_validated_request(cls, value: object) -> DiscoveryRequest:
        if not isinstance(value, DiscoveryRequest):
            raise ValueError("request must be a validated DiscoveryRequest")
        return value

    @field_validator("adapter_key")
    @classmethod
    def validate_adapter(cls, value: str) -> str:
        if value not in ADAPTER_KEYS:
            raise ValueError("unsupported research adapter")
        return value

    @field_validator("adapter_key", mode="before")
    @classmethod
    def reject_adapter_whitespace_variants(cls, value: object) -> object:
        if isinstance(value, str) and value != value.strip():
            raise ValueError("unsupported research adapter")
        return value

    @field_validator("not_before")
    @classmethod
    def normalize_not_before(cls, value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("not_before must be timezone-aware")
        return value.astimezone(timezone.utc)


class ResearchJobRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int
    lead_id: int
    adapter_key: str
    intent_key: str
    request_job_id: str
    request_snapshot: DiscoveryRequest
    request_snapshot_json: str
    target_roles: tuple[str, ...]
    approved_source_types: tuple[str, ...]
    status: ResearchJobStatus
    priority: int
    requested_result_limit: int
    max_pages: int
    max_requests: int
    timeout_seconds: int
    max_attempts: int
    attempt_count: int
    not_before: Optional[datetime]
    requested_by: str
    correlation_id: str
    provider_config_ref: str
    claimed_by: Optional[str]
    lease_token: Optional[str]
    claimed_at: Optional[datetime]
    lease_expires_at: Optional[datetime]
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    result_summary_json: Optional[str]
    safe_error_code: Optional[str]
    retry_after: Optional[datetime]
    version: int
    created_at: datetime
    updated_at: datetime


class ResearchJobListFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True)

    lead_id: Optional[int] = Field(default=None, gt=0)
    status: Optional[ResearchJobStatus] = None
    adapter_key: Optional[str] = None
    limit: int = Field(default=50, ge=1, le=100)

    @field_validator("adapter_key")
    @classmethod
    def validate_adapter(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in ADAPTER_KEYS:
            raise ValueError("unsupported research adapter")
        return value

    @field_validator("adapter_key", mode="before")
    @classmethod
    def reject_adapter_whitespace_variants(cls, value: object) -> object:
        if isinstance(value, str) and value != value.strip():
            raise ValueError("unsupported research adapter")
        return value


def canonical_request_json(request: DiscoveryRequest) -> str:
    """Serialize only the validated request in deterministic canonical JSON."""
    if not isinstance(request, DiscoveryRequest):
        raise ResearchJobError("invalid_request", "research request is not validated")
    text = json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(text) > MAX_SNAPSHOT_LENGTH:
        raise ResearchJobError("invalid_request_snapshot", "research request snapshot is too large")
    return text


def normalized_website(value: str) -> str:
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path.rstrip("/"), parsed.query, parsed.fragment))


def research_intent_key(job: ResearchJobCreate) -> str:
    request = job.request
    intent = {
        "lead_id": request.lead_id,
        "adapter_key": job.adapter_key,
        "normalized_domain": request.normalized_domain,
        "company_website": normalized_website(request.company_website),
        "target_roles": sorted(set(request.target_roles)),
        "approved_source_types": sorted(set(request.approved_source_types)),
        "result_limit": request.result_limit,
        "max_pages": request.max_pages,
        "max_requests": request.max_requests,
        "timeout_seconds": request.timeout_seconds,
        "provider_config_ref": request.provider_config_ref,
    }
    encoded = json.dumps(intent, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
