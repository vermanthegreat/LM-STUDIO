from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import db
import pytest
from pydantic import ValidationError

from discovery_models import DiscoveryOutcome, DiscoveryOutcomeStatus, DiscoveryRequest
from providers.discovery_base import DiscoveryProvider
from providers.fake_discovery import FakeDiscoveryProvider
from research_job_models import ResearchJobError, ResearchJobStatus
from services.research_job_runner import (
    ResearchJobExecutionResult,
    ResearchJobRunner,
    ResearchJobRunnerError,
)
from tests.test_discovery_contracts import outcome as discovery_outcome
from tests.test_research_job_lifecycle import create_job, expire_job, setup_db
from repositories.sqlite_store import SqliteContactStore


NOW = datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc)


def make_runner(tmp_path, outcome=None, *, max_attempts: int = 2, provider=None, clock=None):
    path, lead, *_ = setup_db(tmp_path)
    store = SqliteContactStore(path)
    job = store.enqueue_research_job(create_job(request=create_job().request.model_copy(update={"lead_id": lead["id"]}), max_attempts=max_attempts))
    provider = provider or FakeDiscoveryProvider(outcome or discovery_outcome())
    runner = ResearchJobRunner(store, {"fake": provider}, clock=(lambda: clock) if clock is not None else None)
    return path, store, job, provider, runner


def test_empty_queue_returns_none_without_calling_provider(tmp_path):
    path, lead, *_ = setup_db(tmp_path)
    store = SqliteContactStore(path)

    class Provider:
        def __init__(self):
            self.calls = 0

        def execute(self, request):
            self.calls += 1
            raise AssertionError("provider must not be called")

    provider = Provider()
    runner = ResearchJobRunner(store, {"fake": provider})
    assert runner.run_next(worker_id="worker") is None
    assert provider.calls == 0


def test_registry_is_explicit_and_validated_without_provider_calls():
    class Provider:
        def execute(self, request):
            raise AssertionError

    provider = Provider()
    assert isinstance(provider, DiscoveryProvider)
    ResearchJobRunner(object(), {"fake": provider})
    for registry in ({"": provider}, {"unknown": provider}, {"fake": object()}, {}):
        with pytest.raises(ResearchJobRunnerError) as error:
            ResearchJobRunner(object(), registry)
        assert error.value.code == "invalid_provider_registry"


def test_runner_reconstructs_snapshot_marks_running_before_provider_and_returns_bounded_result(tmp_path):
    path, store, job, provider, runner = make_runner(tmp_path)
    observed = {}

    class OrderedProvider:
        def execute(self, request: DiscoveryRequest) -> DiscoveryOutcome:
            observed["request"] = request
            observed["status"] = store.get_research_job(job.id).status
            with db.get_conn(path) as conn:
                conn.execute("UPDATE leads SET fit_score = fit_score WHERE id = ?", (job.lead_id,))
            observed["outside_transaction"] = True
            return provider.execute(request)

    result = ResearchJobRunner(store, {"fake": OrderedProvider()}, clock=lambda: NOW).run_next(worker_id="worker")
    assert isinstance(result, ResearchJobExecutionResult)
    assert observed["request"] == job.request_snapshot
    assert observed["status"] is ResearchJobStatus.RUNNING
    assert observed["outside_transaction"] is True
    assert result.final_status is ResearchJobStatus.SUCCEEDED
    assert result.provider_status is DiscoveryOutcomeStatus.SUCCEEDED
    assert result.source_count == 1 and result.candidate_count == 1
    assert result.terminal is True and result.retry_scheduled is False
    assert not hasattr(result, "lease_token")
    assert store.get_research_job(job.id).lease_token is None


@pytest.mark.parametrize(
    ("status", "kwargs", "expected"),
    [
        (DiscoveryOutcomeStatus.SUCCEEDED, {}, ResearchJobStatus.SUCCEEDED),
        (DiscoveryOutcomeStatus.PARTIAL, {}, ResearchJobStatus.PARTIAL),
        (DiscoveryOutcomeStatus.NO_RESULT, {"sources": [], "candidates": [], "no_result_reason": "no_result"}, ResearchJobStatus.NO_RESULT),
        (DiscoveryOutcomeStatus.NEEDS_REVIEW, {"sources": [], "candidates": [], "warnings": ["review_required"]}, ResearchJobStatus.NEEDS_REVIEW),
        (DiscoveryOutcomeStatus.PERMANENT_ERROR, {"sources": [], "candidates": [], "safe_error_code": "permanent"}, ResearchJobStatus.FAILED),
        (DiscoveryOutcomeStatus.CANCELLED, {"sources": [], "candidates": []}, ResearchJobStatus.FAILED),
    ],
)
def test_terminal_outcome_mapping_persists_only_compact_summary(tmp_path, status, kwargs, expected):
    path, store, job, _, runner = make_runner(tmp_path, discovery_outcome(status, **kwargs))
    result = runner.run_next(worker_id="worker")
    assert result.final_status is expected
    record = store.get_research_job(job.id)
    summary = json.loads(record.result_summary_json)
    assert set(summary) == {"candidate_count", "note", "reason_code", "source_count", "warning_codes"}
    serialized = record.result_summary_json
    for forbidden in ("Jane Doe", "https://example.com/about", "Visible evidence", "provider-1"):
        assert forbidden not in serialized
    assert record.claimed_by is None and record.lease_token is None


@pytest.mark.parametrize("status", [DiscoveryOutcomeStatus.RATE_LIMITED, DiscoveryOutcomeStatus.RETRYABLE_ERROR])
def test_retryable_outcomes_schedule_retry_and_rate_limit_has_controlled_code(tmp_path, status):
    kwargs = {"sources": [], "candidates": [], "retry_after": datetime.now(timezone.utc) + timedelta(seconds=30)} if status is DiscoveryOutcomeStatus.RATE_LIMITED else {"sources": [], "candidates": [], "safe_error_code": "temporary"}
    path, store, job, _, runner = make_runner(tmp_path, discovery_outcome(status, **kwargs))
    result = runner.run_next(worker_id="worker")
    record = store.get_research_job(job.id)
    assert result.retry_scheduled is True
    assert record.status is ResearchJobStatus.RETRY_WAIT
    assert record.safe_error_code == ("rate_limited" if status is DiscoveryOutcomeStatus.RATE_LIMITED else "temporary")
    assert record.not_before == record.retry_after
    assert record.started_at is None and record.result_summary_json is None


def test_retryable_missing_retry_time_uses_bounded_default_and_exhaustion_fails(tmp_path):
    path, store, job, _, runner = make_runner(tmp_path, discovery_outcome(DiscoveryOutcomeStatus.RETRYABLE_ERROR, sources=[], candidates=[], safe_error_code="temporary"), max_attempts=1)
    result = runner.run_next(worker_id="worker")
    record = store.get_research_job(job.id)
    assert result.final_status is ResearchJobStatus.FAILED
    assert record.status is ResearchJobStatus.FAILED
    assert record.safe_error_code == "temporary"
    assert record.completed_at is not None and record.retry_after is None


def test_provider_exception_retries_once_without_exposing_exception(tmp_path):
    class ExplodingProvider:
        def __init__(self):
            self.calls = 0

        def execute(self, request):
            self.calls += 1
            raise RuntimeError("private provider body and token=secret")

    provider = ExplodingProvider()
    path, store, job, _, runner = make_runner(tmp_path, provider=provider)
    result = runner.run_next(worker_id="worker")
    record = store.get_research_job(job.id)
    assert provider.calls == 1
    assert result.retry_scheduled is True
    assert record.safe_error_code == "provider_exception"
    assert "private provider body" not in json.dumps(record.model_dump(mode="json"))
    assert "secret" not in json.dumps(record.model_dump(mode="json"))


def test_wrong_output_missing_provider_and_invalid_snapshot_fail_without_provider_call(tmp_path):
    class WrongTypeProvider:
        def __init__(self):
            self.calls = 0

        def execute(self, request):
            self.calls += 1
            return {"status": "succeeded"}

    path, store, job, _, runner = make_runner(tmp_path, provider=WrongTypeProvider())
    result = runner.run_next(worker_id="worker")
    assert result.final_status is ResearchJobStatus.FAILED
    assert store.get_research_job(job.id).safe_error_code == "provider_contract_error"

    missing_root = tmp_path / "missing"
    missing_root.mkdir()
    path, store, job, provider, runner = make_runner(missing_root)
    missing_runner = ResearchJobRunner.__new__(ResearchJobRunner)
    # Use a valid runner registry, then remove the explicit adapter to exercise lookup failure.
    missing_runner.repository = store
    missing_runner.providers = {}
    missing_runner.clock = lambda: NOW
    missing_runner.default_retry_delay_seconds = 60
    result = missing_runner.run_next(worker_id="worker")
    assert result.final_status is ResearchJobStatus.FAILED
    assert store.get_research_job(job.id).safe_error_code == "provider_unavailable"

    snapshot_root = tmp_path / "snapshot"
    snapshot_root.mkdir()
    path, store, job, provider, runner = make_runner(snapshot_root)
    inconsistent = job.request_snapshot.model_copy(update={"lead_id": 2})
    with db.get_conn(path) as conn:
        conn.execute("UPDATE research_jobs SET request_snapshot_json = ? WHERE id = ?", (json.dumps(inconsistent.model_dump(mode="json"), sort_keys=True, separators=(",", ":")), job.id))
    result = runner.run_next(worker_id="worker")
    assert result.final_status is ResearchJobStatus.FAILED
    assert provider.requests == []
    assert store.get_research_job(job.id).safe_error_code == "invalid_request_snapshot"


def test_invalid_output_bounds_fail_closed(tmp_path):
    invalid = discovery_outcome(DiscoveryOutcomeStatus.SUCCEEDED)
    invalid.candidates = invalid.candidates * 3
    class InvalidOutcomeProvider:
        def execute(self, request):
            return invalid

    path, store, job, _, runner = make_runner(tmp_path, provider=InvalidOutcomeProvider())
    result = runner.run_next(worker_id="worker")
    assert result.final_status is ResearchJobStatus.FAILED
    assert store.get_research_job(job.id).safe_error_code == "provider_contract_error"


def test_mark_running_failure_prevents_provider_and_lease_errors_propagate(tmp_path):
    path, store, job, provider, runner = make_runner(tmp_path)
    with db.get_conn(path) as conn:
        conn.execute("""CREATE TRIGGER force_runner_mark_running_failure BEFORE UPDATE ON research_jobs
            WHEN OLD.status = 'claimed' AND NEW.status = 'running'
            BEGIN SELECT RAISE(ABORT, 'forced_runner_mark_running_failure'); END""")
    with pytest.raises(ResearchJobError) as error:
        runner.run_next(worker_id="worker")
    assert error.value.code in {"stale_job_version", "lease_update_persistence_failure"}
    assert getattr(provider, "requests", []) == []
    with db.get_conn(path) as conn:
        conn.execute("DROP TRIGGER force_runner_mark_running_failure")


def test_exhausted_provider_exception_uses_lifecycle_failure(tmp_path):
    class ExplodingProvider:
        def execute(self, request):
            raise RuntimeError("do not expose")

    path, store, job, _, runner = make_runner(tmp_path, provider=ExplodingProvider(), max_attempts=1)
    result = runner.run_next(worker_id="worker")
    assert result.final_status is ResearchJobStatus.FAILED
    assert store.get_research_job(job.id).safe_error_code == "provider_exception"


def test_base_exception_is_not_converted_and_stale_recovery_remains_available(tmp_path):
    class CrashProvider:
        def execute(self, request):
            raise KeyboardInterrupt()

    path, store, job, _, runner = make_runner(tmp_path, provider=CrashProvider())
    with pytest.raises(KeyboardInterrupt):
        runner.run_next(worker_id="worker")
    running = store.get_research_job(job.id)
    assert running.status is ResearchJobStatus.RUNNING and running.lease_token is not None
    expire_job(path, job.id)
    recovered = store.recover_stale_research_jobs()
    assert recovered[0].id == job.id


def test_two_runners_race_one_job_and_both_connections_remain_usable(tmp_path):
    path, store, job, _, _ = make_runner(tmp_path)
    calls = []

    class CountingProvider:
        def execute(self, request):
            calls.append(request.job_id)
            return discovery_outcome()

    def run():
        return ResearchJobRunner(SqliteContactStore(path), {"fake": CountingProvider()}, clock=lambda: NOW).run_next(worker_id="race")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert len(calls) == 1
    assert sum(result is not None for result in results) == 1
    with db.get_conn(path) as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1


def test_runner_lifecycle_isolation_preserves_populated_data(tmp_path):
    path, store, job, _, runner = make_runner(tmp_path)
    tables = ("leads", "people", "tasks", "interactions", "person_candidates", "contact_method_candidates", "raw_sources")
    with db.get_conn(path) as conn:
        before = {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")] for table in tables}
    result = runner.run_next(worker_id="worker")
    assert result.final_status is ResearchJobStatus.SUCCEEDED
    with db.get_conn(path) as conn:
        after = {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")] for table in tables}
    assert after == before
