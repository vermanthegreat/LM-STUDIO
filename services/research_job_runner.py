"""Bounded orchestration for one durable research job attempt."""

from __future__ import annotations

import json
import hashlib
import re
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping, Optional

from discovery_models import DiscoveryOutcome, DiscoveryOutcomeStatus, DiscoveryRequest, reject_secrets
from discovery_materialization_models import DiscoveryOutcomeMaterializationResult
from providers.discovery_base import DiscoveryProvider
from research_job_models import (
    ADAPTER_KEYS,
    ResearchJobError,
    ResearchJobFinalization,
    ResearchJobRecord,
    ResearchJobResultSummary,
    ResearchJobRetrySchedule,
    ResearchJobStatus,
)
from repositories import ContactStore


SAFE_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_:-]{0,63}$")
FAKE_ADAPTER_KEY = "fake"
DEFAULT_RETRY_DELAY_SECONDS = 60
MATERIALIZATION_OWNERSHIP_ERRORS = frozenset({
    "job_not_found", "invalid_claim_state", "stale_job_version", "lease_token_mismatch", "lease_expired",
})
MATERIALIZATION_TERMINAL_ERRORS = {
    "outcome_not_materializable": "materialization_contract_error",
    "outcome_exceeds_request_bounds": "materialization_bounds_error",
    "candidate_source_unresolved": "materialization_evidence_error",
    "materialization_conflict": "materialization_conflict",
    "invalid_receipt": "materialization_contract_error",
}
MATERIALIZATION_RETRY_ERRORS = frozenset({
    "source_persistence_failure", "candidate_persistence_failure", "materialization_persistence_failure",
})


class ResearchJobRunnerError(ValueError):
    """Stable orchestration error without provider or lease-sensitive details."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DiscoveryOutcomeMaterializerPort(ABC):
    """Nominal application-owned port for discovery outcome materialization."""

    @abstractmethod
    def materialize_discovery_outcome(
        self,
        *,
        research_job_id: int,
        lease_token: str,
        expected_version: int,
        outcome: DiscoveryOutcome,
    ) -> DiscoveryOutcomeMaterializationResult:
        raise NotImplementedError


class ResearchJobExecutionResult:
    """Bounded immutable operational result returned by the runner."""

    __slots__ = (
        "job_id", "adapter_key", "provider_status", "final_status",
        "source_count", "candidate_count", "retry_scheduled", "terminal",
        "materialized", "materialization_receipt_id", "materialization_replayed",
        "materialized_source_count", "materialized_person_candidate_count",
        "materialized_contact_candidate_count",
    )

    def __init__(
        self,
        *,
        job_id: int,
        adapter_key: str,
        provider_status: Optional[DiscoveryOutcomeStatus],
        final_status: ResearchJobStatus,
        source_count: int,
        candidate_count: int,
        retry_scheduled: bool,
        terminal: bool,
        materialized: bool = False,
        materialization_receipt_id: Optional[int] = None,
        materialization_replayed: bool = False,
        materialized_source_count: int = 0,
        materialized_person_candidate_count: int = 0,
        materialized_contact_candidate_count: int = 0,
    ) -> None:
        self.job_id = job_id
        self.adapter_key = adapter_key
        self.provider_status = provider_status
        self.final_status = final_status
        self.source_count = source_count
        self.candidate_count = candidate_count
        self.retry_scheduled = retry_scheduled
        self.terminal = terminal
        self.materialized = materialized
        self.materialization_receipt_id = materialization_receipt_id
        self.materialization_replayed = materialization_replayed
        self.materialized_source_count = materialized_source_count
        self.materialized_person_candidate_count = materialized_person_candidate_count
        self.materialized_contact_candidate_count = materialized_contact_candidate_count

    def __setattr__(self, name: str, value: object) -> None:
        if hasattr(self, name):
            raise AttributeError("research-job execution result is immutable")
        object.__setattr__(self, name, value)


class ResearchJobRunner:
    """Execute at most one claimed job through an explicit provider registry."""

    def __init__(
        self,
        repository: ContactStore,
        providers: Mapping[str, DiscoveryProvider],
        *,
        materializer: DiscoveryOutcomeMaterializerPort,
        clock: Optional[Callable[[], datetime]] = None,
        default_retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
    ) -> None:
        self.repository = repository
        self.providers = self._validate_registry(providers)
        if not isinstance(materializer, DiscoveryOutcomeMaterializerPort):
            raise ResearchJobRunnerError("invalid_materializer_dependency", "materializer dependency is invalid")
        self._materializer = materializer
        if isinstance(default_retry_delay_seconds, bool) or not isinstance(default_retry_delay_seconds, int) or not 1 <= default_retry_delay_seconds <= 24 * 60 * 60:
            raise ResearchJobRunnerError("invalid_retry_delay", "runner retry delay is invalid")
        self.default_retry_delay_seconds = default_retry_delay_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _validate_registry(providers: Mapping[str, DiscoveryProvider]) -> dict[str, DiscoveryProvider]:
        if not isinstance(providers, Mapping) or not providers:
            raise ResearchJobRunnerError("invalid_provider_registry", "provider registry is invalid")
        validated: dict[str, DiscoveryProvider] = {}
        for key, provider in providers.items():
            if not isinstance(key, str) or not key.strip() or key != key.strip() or key not in ADAPTER_KEYS:
                raise ResearchJobRunnerError("invalid_provider_registry", "provider registry is invalid")
            if key in validated or not isinstance(provider, DiscoveryProvider):
                raise ResearchJobRunnerError("invalid_provider_registry", "provider registry is invalid")
            validated[key] = provider
        return validated

    def run_next(self, *, worker_id: str, lease_seconds: int = 120) -> Optional[ResearchJobExecutionResult]:
        claimed = self.repository.claim_next_research_job(worker_id=worker_id, lease_seconds=lease_seconds)
        if claimed is None:
            return None
        request, snapshot_error = self._reconstruct_request(claimed)
        running = self.repository.mark_research_job_running(
            claimed.id, lease_token=claimed.lease_token, expected_version=claimed.version,
        )
        if snapshot_error is not None:
            return self._finalize_failure(running, "invalid_request_snapshot", snapshot_error, provider_status=None)
        provider = self.providers.get(running.adapter_key)
        if provider is None:
            return self._finalize_failure(running, "provider_unavailable", None, provider_status=None)
        try:
            raw_outcome = provider.execute(request)
        except Exception:
            retry = self._retry_schedule(running, "provider_exception", None)
            final = self.repository.schedule_research_job_retry(
                running.id, lease_token=running.lease_token, expected_version=running.version, retry=retry,
            )
            return self._result(final, None, 0, 0)
        outcome = self._validate_outcome(raw_outcome, request)
        if outcome is None:
            return self._finalize_failure(running, "provider_contract_error", None, provider_status=None)
        try:
            if outcome.status is DiscoveryOutcomeStatus.RATE_LIMITED:
                final = self.repository.schedule_research_job_retry(
                    running.id,
                    lease_token=running.lease_token,
                    expected_version=running.version,
                    retry=self._retry_schedule(running, "rate_limited", outcome.retry_after),
                )
                return self._result(final, outcome.status, len(outcome.sources), len(outcome.candidates))
            if outcome.status is DiscoveryOutcomeStatus.RETRYABLE_ERROR:
                final = self.repository.schedule_research_job_retry(
                    running.id,
                    lease_token=running.lease_token,
                    expected_version=running.version,
                    retry=self._retry_schedule(running, self._safe_code(outcome.safe_error_code, "provider_retryable_error"), outcome.retry_after),
                )
                return self._result(final, outcome.status, len(outcome.sources), len(outcome.candidates))
            if outcome.status in {DiscoveryOutcomeStatus.CANCELLED, DiscoveryOutcomeStatus.PERMANENT_ERROR}:
                final = self.repository.finalize_research_job(
                    running.id,
                    lease_token=running.lease_token,
                    expected_version=running.version,
                    finalization=self._finalization(outcome),
                )
                return self._result(final, outcome.status, len(outcome.sources), len(outcome.candidates))
            try:
                materialization = self._materializer.materialize_discovery_outcome(
                    research_job_id=running.id,
                    lease_token=running.lease_token,
                    expected_version=running.version,
                    outcome=outcome,
                )
                materialization = self._validate_materialization(running, outcome, materialization)
            except ResearchJobError as error:
                if error.code in MATERIALIZATION_OWNERSHIP_ERRORS:
                    raise
                if error.code in MATERIALIZATION_TERMINAL_ERRORS:
                    return self._finalize_failure(
                        running, MATERIALIZATION_TERMINAL_ERRORS[error.code], None,
                        provider_status=outcome.status,
                    )
                if error.code in MATERIALIZATION_RETRY_ERRORS:
                    retry = self._retry_schedule(running, "materialization_persistence_error", None)
                    final = self.repository.schedule_research_job_retry(
                        running.id, lease_token=running.lease_token, expected_version=running.version, retry=retry,
                    )
                    return self._result(final, outcome.status, 0, 0)
                raise
            finalization = self._finalization(outcome, materialization)
            final = self.repository.finalize_research_job(
                running.id, lease_token=running.lease_token, expected_version=running.version, finalization=finalization,
            )
            return self._result(final, outcome.status, finalization.summary.source_count, finalization.summary.candidate_count, materialization)
        except ResearchJobError:
            raise

    def _reconstruct_request(self, job: ResearchJobRecord) -> tuple[Optional[DiscoveryRequest], Optional[str]]:
        try:
            raw = json.loads(job.request_snapshot_json)
            request = DiscoveryRequest.model_validate(raw)
            if json.dumps(request.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")) != json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")):
                raise ValueError("snapshot is not canonical")
            if (
                request.job_id != job.request_job_id
                or request.lead_id != job.lead_id
                or request.result_limit != job.requested_result_limit
                or request.max_pages != job.max_pages
                or request.max_requests != job.max_requests
                or request.timeout_seconds != job.timeout_seconds
                or tuple(sorted(request.target_roles)) != job.target_roles
                or tuple(sorted(request.approved_source_types)) != job.approved_source_types
                or request.requester_identity != job.requested_by
                or request.correlation_id != job.correlation_id
                or request.provider_config_ref != job.provider_config_ref
            ):
                raise ValueError("snapshot is inconsistent")
            return request, None
        except Exception:
            return None, "invalid request snapshot"

    @staticmethod
    def _validate_outcome(value: object, request: DiscoveryRequest) -> Optional[DiscoveryOutcome]:
        if not isinstance(value, DiscoveryOutcome):
            return None
        try:
            outcome = DiscoveryOutcome.model_validate(value.model_dump(mode="python"))
            return outcome.validate_for_request(request)
        except Exception:
            return None

    def _finalization(self, outcome: DiscoveryOutcome, materialization: Optional[DiscoveryOutcomeMaterializationResult] = None) -> ResearchJobFinalization:
        source_count = len(materialization.raw_source_ids) if materialization is not None else len(outcome.sources)
        candidate_count = len(materialization.person_candidate_ids) if materialization is not None else len(outcome.candidates)
        warning_codes = self._warning_codes(outcome.warnings)
        if outcome.status is DiscoveryOutcomeStatus.SUCCEEDED:
            return ResearchJobFinalization(status="succeeded", summary=ResearchJobResultSummary(source_count=source_count, candidate_count=candidate_count, warning_codes=warning_codes))
        if outcome.status is DiscoveryOutcomeStatus.PARTIAL:
            return ResearchJobFinalization(status="partial", summary=ResearchJobResultSummary(source_count=source_count, candidate_count=candidate_count, warning_codes=warning_codes or ("partial",)))
        if outcome.status is DiscoveryOutcomeStatus.NO_RESULT:
            return ResearchJobFinalization(status="no_result", summary=ResearchJobResultSummary(source_count=source_count, candidate_count=0, warning_codes=warning_codes, reason_code=self._safe_code(outcome.no_result_reason, "no_result")))
        if outcome.status is DiscoveryOutcomeStatus.NEEDS_REVIEW:
            return ResearchJobFinalization(status="needs_review", summary=ResearchJobResultSummary(source_count=source_count, candidate_count=candidate_count, warning_codes=warning_codes or ("review_required",)))
        if outcome.status is DiscoveryOutcomeStatus.CANCELLED:
            return ResearchJobFinalization(status="failed", summary=ResearchJobResultSummary(source_count=0, candidate_count=0), safe_error_code="provider_cancelled")
        if outcome.status is DiscoveryOutcomeStatus.PERMANENT_ERROR:
            return ResearchJobFinalization(status="failed", summary=ResearchJobResultSummary(source_count=0, candidate_count=0), safe_error_code=self._safe_code(outcome.safe_error_code, "provider_permanent_error"))
        raise ResearchJobRunnerError("provider_contract_error", "provider outcome status is not terminal")

    @staticmethod
    def _expected_outcome_digest(outcome: DiscoveryOutcome) -> str:
        canonical = json.dumps(outcome.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def _validate_materialization(
        cls,
        job: ResearchJobRecord,
        outcome: DiscoveryOutcome,
        value: object,
    ) -> DiscoveryOutcomeMaterializationResult:
        if not isinstance(value, DiscoveryOutcomeMaterializationResult):
            raise ResearchJobRunnerError("materialization_result_mismatch", "materialization result is invalid")
        if (
            value.research_job_id != job.id
            or value.attempt_count != job.attempt_count
            or value.outcome_status is not outcome.status
            or value.outcome_digest != cls._expected_outcome_digest(outcome)
            or value.materialization_id <= 0
            or any(not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in (*value.raw_source_ids, *value.person_candidate_ids, *value.contact_candidate_ids))
            or len(value.raw_source_ids) != len(outcome.sources)
            or len(value.person_candidate_ids) != len(outcome.candidates)
            or len(value.contact_candidate_ids) != sum(len(candidate.explicit_contacts) for candidate in outcome.candidates)
        ):
            raise ResearchJobRunnerError("materialization_result_mismatch", "materialization result is inconsistent")
        return value

    def _retry_schedule(self, job: ResearchJobRecord, code: str, retry_at: Optional[datetime]) -> ResearchJobRetrySchedule:
        now = self._now()
        target = retry_at or (now + timedelta(seconds=self.default_retry_delay_seconds))
        return ResearchJobRetrySchedule(safe_error_code=code, retry_at=target)

    def _finalize_failure(self, job: ResearchJobRecord, code: str, _detail: Optional[str], *, provider_status: Optional[DiscoveryOutcomeStatus]) -> ResearchJobExecutionResult:
        final = self.repository.finalize_research_job(
            job.id,
            lease_token=job.lease_token,
            expected_version=job.version,
            finalization=ResearchJobFinalization(status="failed", summary=ResearchJobResultSummary(source_count=0, candidate_count=0), safe_error_code=code),
        )
        return self._result(final, provider_status, 0, 0)

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ResearchJobRunnerError("invalid_runner_clock", "runner clock is invalid")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _safe_code(value: Optional[str], fallback: str) -> str:
        if isinstance(value, str):
            cleaned = value.strip().casefold()
            try:
                reject_secrets(cleaned)
            except ValueError:
                return fallback
            if SAFE_CODE_RE.fullmatch(cleaned):
                return cleaned
        return fallback

    @classmethod
    def _warning_codes(cls, values: list[str]) -> tuple[str, ...]:
        return tuple(sorted({code for code in (cls._safe_code(value, "") for value in values) if code}))

    @staticmethod
    def _result(job: ResearchJobRecord, provider_status: Optional[DiscoveryOutcomeStatus], source_count: int, candidate_count: int, materialization: Optional[DiscoveryOutcomeMaterializationResult] = None) -> ResearchJobExecutionResult:
        return ResearchJobExecutionResult(
            job_id=job.id,
            adapter_key=job.adapter_key,
            provider_status=provider_status,
            final_status=job.status,
            source_count=source_count,
            candidate_count=candidate_count,
            retry_scheduled=job.status is ResearchJobStatus.RETRY_WAIT,
            terminal=job.status in {
                ResearchJobStatus.SUCCEEDED, ResearchJobStatus.PARTIAL, ResearchJobStatus.NO_RESULT,
                ResearchJobStatus.NEEDS_REVIEW, ResearchJobStatus.FAILED, ResearchJobStatus.CANCELLED,
                ResearchJobStatus.ABANDONED,
            },
            materialized=materialization is not None,
            materialization_receipt_id=materialization.materialization_id if materialization is not None else None,
            materialization_replayed=materialization.replayed if materialization is not None else False,
            materialized_source_count=len(materialization.raw_source_ids) if materialization is not None else 0,
            materialized_person_candidate_count=len(materialization.person_candidate_ids) if materialization is not None else 0,
            materialized_contact_candidate_count=len(materialization.contact_candidate_ids) if materialization is not None else 0,
        )
