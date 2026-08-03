"""Typed, durable models for bounded contact-research jobs."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from discovery_models import DiscoveryRequest


ADAPTER_KEYS = ("fake",)
RESEARCH_JOB_ACTIVE_STATES = ("queued", "claimed", "running", "retry_wait")
RESEARCH_JOB_TERMINAL_STATES = (
    "succeeded", "partial", "no_result", "needs_review", "failed", "cancelled", "abandoned",
)
MAX_PRIORITY = 100
MAX_SNAPSHOT_LENGTH = 20_000


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


class ResearchJobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

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
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

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
