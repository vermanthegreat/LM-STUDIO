from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import db
import pytest
from pydantic import ValidationError

from repositories.sqlite_store import SqliteContactStore
from research_job_models import (
    ResearchJobError,
    ResearchJobCancellation,
    ResearchJobFinalization,
    ResearchJobResultSummary,
    ResearchJobRetrySchedule,
    ResearchJobStatus,
    ResearchJobTerminalStatus,
)
from tests.test_research_job_persistence import create_job, setup_db


def running_job(tmp_path, *, max_attempts: int = 2):
    path, lead, *_ = setup_db(tmp_path)
    store = SqliteContactStore(path)
    record = store.enqueue_research_job(create_job(request=create_job().request.model_copy(update={"lead_id": lead["id"]}), max_attempts=max_attempts))
    claimed = store.claim_next_research_job(worker_id="worker")
    assert claimed is not None
    running = store.mark_research_job_running(claimed.id, lease_token=claimed.lease_token, expected_version=claimed.version)
    return path, store, running


def job_variant(lead_id: int, index: int):
    roles = ["economic_buyer", "operational_owner", "technical_influencer", "workflow_user", "other"]
    request = create_job().request.model_copy(update={
        "lead_id": lead_id,
        "job_id": f"lifecycle-{index}",
        "target_roles": [roles[index % len(roles)]],
        "normalized_domain": f"example-{index}.com",
        "company_website": f"https://example-{index}.com/",
    })
    return create_job(request=request)


def expire_job(path, job_id: int, *, started: bool = False, attempt_count: int | None = None, expiry_offset: int = -1, updated_offset: int = 0):
    expired = (datetime.now(timezone.utc) + timedelta(seconds=expiry_offset)).isoformat()
    updated = (datetime.now(timezone.utc) + timedelta(seconds=updated_offset)).isoformat()
    assignments = ["lease_expires_at = ?", "updated_at = ?"]
    values: list[object] = [expired, updated]
    if attempt_count is not None:
        assignments.append("attempt_count = ?")
        values.append(attempt_count)
    with db.get_conn(path) as conn:
        conn.execute(f"UPDATE research_jobs SET {', '.join(assignments)} WHERE id = ?", (*values, job_id))


def research_job_fields(record):
    return record.model_dump(mode="json")


def valid_finalization(status: ResearchJobTerminalStatus) -> ResearchJobFinalization:
    summaries = {
        ResearchJobTerminalStatus.SUCCEEDED: ResearchJobResultSummary(source_count=1, candidate_count=1),
        ResearchJobTerminalStatus.PARTIAL: ResearchJobResultSummary(source_count=1, candidate_count=0, warning_codes=("partial",)),
        ResearchJobTerminalStatus.NO_RESULT: ResearchJobResultSummary(source_count=0, candidate_count=0, reason_code="no_result"),
        ResearchJobTerminalStatus.NEEDS_REVIEW: ResearchJobResultSummary(source_count=0, candidate_count=0, reason_code="review_required"),
        ResearchJobTerminalStatus.FAILED: ResearchJobResultSummary(source_count=0, candidate_count=0),
    }
    return ResearchJobFinalization(status=status, summary=summaries[status], safe_error_code="provider_failed" if status is ResearchJobTerminalStatus.FAILED else None)


def test_finalization_is_typed_bounded_canonical_and_lease_guarded(tmp_path):
    path, store, running = running_job(tmp_path)
    finalization = ResearchJobFinalization(
        status="succeeded",
        summary=ResearchJobResultSummary(source_count=1, candidate_count=1, warning_codes=("late", "late")),
    )
    finished = store.finalize_research_job(running.id, lease_token=running.lease_token, expected_version=running.version, finalization=finalization)
    assert finished.status is ResearchJobStatus.SUCCEEDED
    assert json.loads(finished.result_summary_json) == {
        "candidate_count": 1, "note": None, "reason_code": None, "source_count": 1, "warning_codes": ["late"],
        "underlying_result_code": None,
    }
    assert finished.started_at == running.started_at
    assert finished.completed_at is not None
    assert finished.attempt_count == running.attempt_count
    assert finished.version == running.version + 1
    assert finished.lease_token is None and finished.claimed_by is None
    with pytest.raises(ResearchJobError) as replay:
        store.finalize_research_job(running.id, lease_token=running.lease_token, expected_version=running.version, finalization=finalization)
    assert replay.value.code == "invalid_claim_state"


def test_retry_wait_and_exhausted_retry_are_controlled(tmp_path):
    path, store, running = running_job(tmp_path, max_attempts=2)
    retry = ResearchJobRetrySchedule(safe_error_code="rate_limited", retry_at=datetime.now(timezone.utc) + timedelta(minutes=1), reason="provider asked us to wait")
    waiting = store.schedule_research_job_retry(running.id, lease_token=running.lease_token, expected_version=running.version, retry=retry)
    assert waiting.status is ResearchJobStatus.RETRY_WAIT
    assert waiting.not_before == waiting.retry_after == retry.retry_at
    assert waiting.started_at is None and waiting.attempt_count == running.attempt_count
    with db.get_conn(path) as conn:
        conn.execute("UPDATE research_jobs SET not_before = ?, retry_after = ? WHERE id = ?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), waiting.id))
    claimed = store.claim_next_research_job(worker_id="worker-2")
    assert claimed is not None
    running_again = store.mark_research_job_running(claimed.id, lease_token=claimed.lease_token, expected_version=claimed.version)
    exhausted = store.schedule_research_job_retry(
        running_again.id, lease_token=running_again.lease_token, expected_version=running_again.version,
        retry=ResearchJobRetrySchedule(safe_error_code="rate_limited", retry_at=datetime.now(timezone.utc) + timedelta(minutes=1)),
    )
    assert exhausted.status is ResearchJobStatus.FAILED
    assert exhausted.retry_after is None and exhausted.completed_at is not None
    assert exhausted.started_at == running_again.started_at


def test_cancellation_releases_active_intent_and_preserves_data(tmp_path):
    path, lead, *_ = setup_db(tmp_path)
    store = SqliteContactStore(path)
    original = store.enqueue_research_job(create_job(request=create_job().request.model_copy(update={"lead_id": lead["id"]})))
    cancelled = store.cancel_research_job(original.id, expected_version=original.version)
    assert cancelled.status is ResearchJobStatus.CANCELLED
    replacement = store.enqueue_research_job(create_job(request=create_job().request.model_copy(update={"lead_id": lead["id"], "job_id": "replacement"})))
    assert replacement.id != original.id
    with pytest.raises(ResearchJobError) as second:
        store.cancel_research_job(original.id, expected_version=cancelled.version)
    assert second.value.code == "invalid_cancellation_state"


def test_stale_recovery_is_atomic_and_two_connections_do_not_double_recover(tmp_path):
    path, store, running = running_job(tmp_path)
    with db.get_conn(path) as conn:
        conn.execute("UPDATE research_jobs SET lease_expires_at = ?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
    def recover():
        return SqliteContactStore(path).recover_stale_research_jobs(limit=20)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: recover(), range(2)))
    recovered = [row for result in results for row in result]
    assert len(recovered) == 1
    assert recovered[0].status is ResearchJobStatus.RETRY_WAIT
    assert store.get_research_job(running.id).version == running.version + 1


def test_lifecycle_rejects_secrets_and_invalid_bounds_without_echoing_values(tmp_path):
    with pytest.raises(ValueError):
        ResearchJobResultSummary(source_count=0, candidate_count=0, note="authorization: very-secret-value")
    with pytest.raises(ValueError):
        ResearchJobRetrySchedule(safe_error_code="x", retry_at=datetime.now(timezone.utc), reason="token=very-secret-value")
    with pytest.raises(ValueError):
        ResearchJobFinalization(status="succeeded", summary=ResearchJobResultSummary(source_count=0, candidate_count=0))
    assert ResearchJobResultSummary(source_count=0, candidate_count=0, note="temporary provider outage").note == "temporary provider outage"


@pytest.mark.parametrize("status", list(ResearchJobTerminalStatus))
def test_all_terminal_statuses_finalize_and_preserve_job_identity(tmp_path, status):
    path, store, running = running_job(tmp_path)
    before = research_job_fields(running)
    finalization = valid_finalization(status)
    finished = store.finalize_research_job(running.id, lease_token=running.lease_token, expected_version=running.version, finalization=finalization)
    assert finished.status.value == status.value
    assert finished.completed_at is not None
    assert finished.version == running.version + 1
    assert finished.attempt_count == running.attempt_count
    assert finished.started_at == running.started_at
    assert all(getattr(finished, field) is None for field in ("claimed_by", "lease_token", "claimed_at", "lease_expires_at", "not_before", "retry_after"))
    for field in ("lead_id", "adapter_key", "intent_key", "request_snapshot_json", "target_roles", "approved_source_types", "priority", "max_attempts", "requested_by", "correlation_id", "provider_config_ref", "created_at"):
        assert research_job_fields(finished)[field] == before[field]
    assert ResearchJobResultSummary.model_validate_json(finished.result_summary_json) == finalization.summary


def test_status_specific_summary_invariants_are_directly_enforced():
    with pytest.raises(ValidationError):
        ResearchJobFinalization(status="succeeded", summary=ResearchJobResultSummary(source_count=0, candidate_count=0))
    with pytest.raises(ValidationError):
        ResearchJobFinalization(status="succeeded", summary=ResearchJobResultSummary(source_count=1, candidate_count=1), safe_error_code="failed")
    with pytest.raises(ValidationError):
        ResearchJobFinalization(status="partial", summary=ResearchJobResultSummary(source_count=0, candidate_count=0))
    with pytest.raises(ValidationError):
        ResearchJobFinalization(status="partial", summary=ResearchJobResultSummary(source_count=1, candidate_count=0))
    with pytest.raises(ValidationError):
        ResearchJobFinalization(status="partial", summary=ResearchJobResultSummary(source_count=1, candidate_count=0), safe_error_code="failed")
    with pytest.raises(ValidationError):
        ResearchJobFinalization(status="no_result", summary=ResearchJobResultSummary(source_count=0, candidate_count=1, reason_code="unexpected"))
    with pytest.raises(ValidationError):
        ResearchJobFinalization(status="no_result", summary=ResearchJobResultSummary(source_count=0, candidate_count=0))
    with pytest.raises(ValidationError):
        ResearchJobFinalization(status="needs_review", summary=ResearchJobResultSummary(source_count=0, candidate_count=0))
    with pytest.raises(ValidationError):
        ResearchJobFinalization(status="needs_review", summary=ResearchJobResultSummary(source_count=0, candidate_count=0, reason_code="review"), safe_error_code="failed")
    with pytest.raises(ValidationError):
        ResearchJobFinalization(status="failed", summary=ResearchJobResultSummary(source_count=0, candidate_count=0))
    with pytest.raises(ValidationError):
        ResearchJobFinalization(status="failed", summary=ResearchJobResultSummary(source_count=0, candidate_count=1), safe_error_code="failed")
    assert {item.value for item in ResearchJobTerminalStatus} == {"succeeded", "partial", "no_result", "needs_review", "failed"}


def test_result_summary_bounds_canonicalization_and_round_trip(tmp_path):
    path, store, running = running_job(tmp_path)
    with pytest.raises(ValidationError):
        ResearchJobResultSummary(source_count=-1, candidate_count=0)
    with pytest.raises(ValidationError):
        ResearchJobResultSummary(source_count=0, candidate_count=-1)
    with pytest.raises(ValidationError):
        ResearchJobResultSummary(source_count=0, candidate_count=1, extra_field="nope")
    with pytest.raises(ValidationError):
        ResearchJobResultSummary(source_count=0, candidate_count=1, metadata={"nested": "nope"})
    normalized = ResearchJobResultSummary(source_count=1, candidate_count=1, warning_codes=("zeta", "alpha", "zeta"), reason_code="No_Result")
    assert normalized.warning_codes == ("alpha", "zeta")
    assert normalized.reason_code == "no_result"
    assert normalized.canonical_json() == normalized.canonical_json()
    assert ResearchJobResultSummary.model_validate_json(normalized.canonical_json()) == normalized
    legacy = ResearchJobResultSummary.model_validate_json(
        '{"candidate_count":0,"note":null,"reason_code":null,"source_count":0,"warning_codes":[]}'
    )
    assert legacy.underlying_result_code is None
    with pytest.raises(ResearchJobError) as candidate_bound:
        store.finalize_research_job(running.id, lease_token=running.lease_token, expected_version=running.version, finalization=ResearchJobFinalization(status="succeeded", summary=ResearchJobResultSummary(source_count=1, candidate_count=3)))
    assert candidate_bound.value.code == "result_count_exceeds_request_bounds"
    with pytest.raises(ResearchJobError) as source_bound:
        store.finalize_research_job(running.id, lease_token=running.lease_token, expected_version=running.version, finalization=ResearchJobFinalization(status="succeeded", summary=ResearchJobResultSummary(source_count=3, candidate_count=1)))
    assert source_bound.value.code == "result_count_exceeds_request_bounds"
    assert store.get_research_job(running.id).status is ResearchJobStatus.RUNNING


def test_result_summary_secret_matrix_rejects_without_echoing_values():
    rejected = (
        "Bearer abc123", "Basic abc123", "api_key=abc123", "token=abc123",
        "password=abc123", "secret: abc123", "cookie=abc123", "session=abc123",
        "https://example.com/?api_key=abc123", {"nested": {"authorization": "Bearer abc123"}},
    )
    for value in rejected[:-1]:
        with pytest.raises((ValidationError, ValueError)) as error:
            ResearchJobResultSummary(source_count=0, candidate_count=0, note=value)
        rendered = " ".join((str(error.value), repr(error.value), repr(error.value.errors()), json.dumps(error.value.errors(), default=str)))
        assert "abc123" not in rendered
    with pytest.raises((ValidationError, ValueError)):
        ResearchJobResultSummary(source_count=0, candidate_count=0, note={"nested": {"authorization": "Bearer abc123"}})
    assert ResearchJobResultSummary(source_count=0, candidate_count=0, note="The token terminology is ordinary prose.").note
    assert ResearchJobResultSummary(source_count=0, candidate_count=0, note="hash 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef").note


def test_nested_lifecycle_validation_errors_hide_rejected_values():
    cases = (
        lambda: ResearchJobFinalization(status="partial", summary={"source_count": 1, "candidate_count": 0, "note": "token=abc123", "warning_codes": ["partial"]}),
        lambda: ResearchJobRetrySchedule(safe_error_code="temporary", retry_at=datetime.now(timezone.utc), reason="Bearer abc123"),
        lambda: ResearchJobCancellation(reason_code="token=abc123"),
    )
    for construct in cases:
        with pytest.raises(ValidationError) as error:
            construct()
        rendered = " ".join((str(error.value), repr(error.value), repr(error.value.errors()), json.dumps(error.value.errors(), default=str)))
        assert "abc123" not in rendered


@pytest.mark.parametrize("state", ["queued", "retry_wait", "claimed", "succeeded", "partial", "no_result", "needs_review", "failed", "cancelled", "abandoned"])
def test_finalization_guard_matrix_rejects_and_preserves_record(tmp_path, state):
    path, store, running = running_job(tmp_path)
    if state == "running":
        target = running
    else:
        with db.get_conn(path) as conn:
            conn.execute("UPDATE research_jobs SET status = ?, claimed_by = NULL, lease_token = NULL, claimed_at = NULL, lease_expires_at = NULL WHERE id = ?", (state, running.id))
        target = store.get_research_job(running.id)
    before = research_job_fields(target)
    with pytest.raises(ResearchJobError) as error:
        store.finalize_research_job(target.id, lease_token=target.lease_token or "wrong", expected_version=target.version, finalization=valid_finalization(ResearchJobTerminalStatus.SUCCEEDED))
    assert error.value.code == "invalid_claim_state"
    assert research_job_fields(store.get_research_job(target.id)) == before


def test_finalization_guard_matrix_distinguishes_missing_token_version_and_expiry(tmp_path):
    path, store, running = running_job(tmp_path)
    before = research_job_fields(running)
    with pytest.raises(ResearchJobError) as missing:
        store.finalize_research_job(99999, lease_token="wrong", expected_version=1, finalization=valid_finalization(ResearchJobTerminalStatus.SUCCEEDED))
    assert missing.value.code == "job_not_found"
    with pytest.raises(ResearchJobError) as token:
        store.finalize_research_job(running.id, lease_token="wrong", expected_version=running.version, finalization=valid_finalization(ResearchJobTerminalStatus.SUCCEEDED))
    assert token.value.code == "lease_token_mismatch"
    with pytest.raises(ResearchJobError) as stale:
        store.finalize_research_job(running.id, lease_token=running.lease_token, expected_version=running.version - 1, finalization=valid_finalization(ResearchJobTerminalStatus.SUCCEEDED))
    assert stale.value.code == "stale_job_version"
    expire_job(path, running.id)
    expired = store.get_research_job(running.id)
    with pytest.raises(ResearchJobError) as lease:
        store.finalize_research_job(expired.id, lease_token=expired.lease_token, expected_version=expired.version, finalization=valid_finalization(ResearchJobTerminalStatus.SUCCEEDED))
    assert lease.value.code == "lease_expired"
    assert research_job_fields(store.get_research_job(running.id)) != {}
    assert before["version"] == running.version


def test_forced_finalization_rolls_back_every_field_and_connection_recovers(tmp_path):
    path, store, running = running_job(tmp_path)
    before = research_job_fields(running)
    with db.get_conn(path) as conn:
        conn.execute("""CREATE TRIGGER force_research_job_finalization_failure BEFORE UPDATE ON research_jobs
            WHEN OLD.status = 'running' AND NEW.status IN ('succeeded', 'partial', 'no_result', 'needs_review', 'failed')
            BEGIN SELECT RAISE(ABORT, 'forced_finalization_failure'); END""")
    with pytest.raises(ResearchJobError) as error:
        store.finalize_research_job(running.id, lease_token=running.lease_token, expected_version=running.version, finalization=valid_finalization(ResearchJobTerminalStatus.SUCCEEDED))
    assert error.value.code == "finalization_persistence_failure"
    assert "forced_finalization_failure" not in str(error.value)
    assert running.lease_token not in str(error.value)
    assert research_job_fields(store.get_research_job(running.id)) == before
    with db.get_conn(path) as conn:
        conn.execute("DROP TRIGGER force_research_job_finalization_failure")
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    assert store.finalize_research_job(running.id, lease_token=running.lease_token, expected_version=running.version, finalization=valid_finalization(ResearchJobTerminalStatus.SUCCEEDED)).status is ResearchJobStatus.SUCCEEDED


def test_retry_success_bounds_guards_and_rate_limit_representation(tmp_path):
    path, store, running = running_job(tmp_path)
    retry_at = datetime.now(timezone.utc) + timedelta(minutes=1)
    retry = ResearchJobRetrySchedule(safe_error_code="rate_limited", retry_at=retry_at, reason="provider requested a short wait")
    before = research_job_fields(running)
    waiting = store.schedule_research_job_retry(running.id, lease_token=running.lease_token, expected_version=running.version, retry=retry)
    assert waiting.status is ResearchJobStatus.RETRY_WAIT
    assert waiting.not_before == waiting.retry_after == retry.retry_at
    assert waiting.safe_error_code == "rate_limited"
    assert waiting.started_at is None and waiting.completed_at is None and waiting.result_summary_json is None
    assert waiting.attempt_count == running.attempt_count and waiting.version == running.version + 1
    assert all(getattr(waiting, field) is None for field in ("claimed_by", "lease_token", "claimed_at", "lease_expires_at"))
    for field in ("lead_id", "intent_key", "request_snapshot_json", "adapter_key", "priority", "max_attempts", "requested_by", "correlation_id", "provider_config_ref"):
        assert research_job_fields(waiting)[field] == before[field]

    for index, bad_time in enumerate((datetime.now(timezone.utc) - timedelta(seconds=1), datetime.now(timezone.utc) + timedelta(hours=24, seconds=1))):
        fresh_root = tmp_path / str(index)
        fresh_root.mkdir(exist_ok=True)
        fresh_path, fresh_store, fresh_running = running_job(fresh_root)
        with pytest.raises(ResearchJobError) as error:
            fresh_store.schedule_research_job_retry(fresh_running.id, lease_token=fresh_running.lease_token, expected_version=fresh_running.version, retry=ResearchJobRetrySchedule(safe_error_code="temporary", retry_at=bad_time))
        assert error.value.code == "retry_time_out_of_bounds"
        assert research_job_fields(fresh_store.get_research_job(fresh_running.id)) == research_job_fields(fresh_running)
    with pytest.raises(ValidationError):
        ResearchJobRetrySchedule(safe_error_code="temporary", retry_at=datetime.now())
    with pytest.raises(ValidationError):
        ResearchJobRetrySchedule(safe_error_code="temporary", retry_at=retry_at, reason="token=secret")


@pytest.mark.parametrize("state", ["queued", "retry_wait", "claimed", "succeeded", "partial", "no_result", "needs_review", "failed", "cancelled", "abandoned"])
def test_retry_guard_matrix_rejects_and_preserves_record(tmp_path, state):
    path, store, running = running_job(tmp_path)
    if state == "claimed":
        with db.get_conn(path) as conn:
            conn.execute("UPDATE research_jobs SET status = 'claimed' WHERE id = ?", (running.id,))
    else:
        with db.get_conn(path) as conn:
            conn.execute("UPDATE research_jobs SET status = ?, claimed_by = NULL, lease_token = NULL, claimed_at = NULL, lease_expires_at = NULL WHERE id = ?", (state, running.id))
    target = store.get_research_job(running.id)
    before = research_job_fields(target)
    with pytest.raises(ResearchJobError) as error:
        store.schedule_research_job_retry(target.id, lease_token=target.lease_token or "wrong", expected_version=target.version, retry=ResearchJobRetrySchedule(safe_error_code="temporary", retry_at=datetime.now(timezone.utc) + timedelta(minutes=1)))
    assert error.value.code == "invalid_claim_state"
    assert research_job_fields(store.get_research_job(target.id)) == before


def test_exhausted_retry_becomes_failed_without_new_job(tmp_path):
    path, store, running = running_job(tmp_path, max_attempts=1)
    assert running.attempt_count == running.max_attempts
    before_count = len(store.list_research_jobs())
    result = store.schedule_research_job_retry(running.id, lease_token=running.lease_token, expected_version=running.version, retry=ResearchJobRetrySchedule(safe_error_code="rate_limited", retry_at=datetime.now(timezone.utc) + timedelta(minutes=1)))
    assert result.status is ResearchJobStatus.FAILED
    assert result.completed_at is not None and result.started_at == running.started_at
    assert result.safe_error_code == "rate_limited"
    assert result.not_before is None and result.retry_after is None
    assert result.attempt_count == running.attempt_count and result.version == running.version + 1
    assert all(getattr(result, field) is None for field in ("claimed_by", "lease_token", "claimed_at", "lease_expires_at"))
    assert len(store.list_research_jobs()) == before_count


def test_forced_retry_rolls_back_and_later_retry_succeeds(tmp_path):
    path, store, running = running_job(tmp_path)
    before = research_job_fields(running)
    with db.get_conn(path) as conn:
        conn.execute("""CREATE TRIGGER force_research_job_retry_failure BEFORE UPDATE ON research_jobs
            WHEN OLD.status = 'running' AND NEW.status IN ('retry_wait', 'failed')
            BEGIN SELECT RAISE(ABORT, 'forced_retry_failure'); END""")
    with pytest.raises(ResearchJobError) as error:
        store.schedule_research_job_retry(running.id, lease_token=running.lease_token, expected_version=running.version, retry=ResearchJobRetrySchedule(safe_error_code="temporary", retry_at=datetime.now(timezone.utc) + timedelta(minutes=1)))
    assert error.value.code == "retry_persistence_failure"
    assert "forced_retry_failure" not in str(error.value) and running.lease_token not in str(error.value)
    assert research_job_fields(store.get_research_job(running.id)) == before
    with db.get_conn(path) as conn:
        conn.execute("DROP TRIGGER force_research_job_retry_failure")
    assert store.schedule_research_job_retry(running.id, lease_token=running.lease_token, expected_version=running.version, retry=ResearchJobRetrySchedule(safe_error_code="temporary", retry_at=datetime.now(timezone.utc) + timedelta(minutes=1))).status is ResearchJobStatus.RETRY_WAIT


@pytest.mark.parametrize("retry_wait", [False, True])
def test_queued_and_retry_wait_cancellation_preserve_request_and_release_intent(tmp_path, retry_wait):
    path, lead, *_ = setup_db(tmp_path)
    store = SqliteContactStore(path)
    original = store.enqueue_research_job(create_job(request=create_job().request.model_copy(update={"lead_id": lead["id"]})))
    if retry_wait:
        with db.get_conn(path) as conn:
            conn.execute("UPDATE research_jobs SET status = 'retry_wait', not_before = ?, retry_after = ? WHERE id = ?", ((datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(), (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(), original.id))
    before = research_job_fields(store.get_research_job(original.id))
    cancelled = store.cancel_research_job(original.id, expected_version=original.version, reason_code="operator_review" if retry_wait else None)
    assert cancelled.status is ResearchJobStatus.CANCELLED
    assert cancelled.safe_error_code == ("operator_review" if retry_wait else "cancelled_by_operator")
    assert cancelled.completed_at is not None and cancelled.started_at is None
    assert cancelled.not_before is None and cancelled.retry_after is None
    assert cancelled.lease_token is None and cancelled.claimed_by is None
    assert cancelled.version == original.version + 1
    for field in ("lead_id", "adapter_key", "intent_key", "request_snapshot_json", "target_roles", "approved_source_types", "priority", "max_attempts", "attempt_count", "requested_by", "correlation_id", "provider_config_ref", "created_at"):
        assert research_job_fields(cancelled)[field] == before[field]
    replacement = store.enqueue_research_job(create_job(request=create_job().request.model_copy(update={"lead_id": lead["id"], "job_id": "after-cancel"})))
    assert replacement.id != cancelled.id and replacement.status is ResearchJobStatus.QUEUED
    assert len(store.list_research_jobs()) == 2


@pytest.mark.parametrize("state", ["claimed", "running", "succeeded", "partial", "no_result", "needs_review", "failed", "cancelled", "abandoned"])
def test_cancellation_guard_matrix_and_replay(tmp_path, state):
    path, store, running = running_job(tmp_path)
    if state == "running":
        target = running
    else:
        with db.get_conn(path) as conn:
            conn.execute("UPDATE research_jobs SET status = ?, claimed_by = NULL, lease_token = NULL, claimed_at = NULL, lease_expires_at = NULL WHERE id = ?", (state, running.id))
        target = store.get_research_job(running.id)
    before = research_job_fields(target)
    with pytest.raises(ResearchJobError) as error:
        store.cancel_research_job(target.id, expected_version=target.version)
    assert error.value.code == "invalid_cancellation_state"
    assert research_job_fields(store.get_research_job(target.id)) == before
    if state == "queued":
        cancelled = store.cancel_research_job(target.id, expected_version=target.version)
        with pytest.raises(ResearchJobError) as replay:
            store.cancel_research_job(cancelled.id, expected_version=cancelled.version)
        assert replay.value.code == "invalid_cancellation_state"


def test_cancellation_rejects_missing_stale_and_secret_reason(tmp_path):
    path, store, running = running_job(tmp_path)
    with pytest.raises(ResearchJobError) as missing:
        store.cancel_research_job(99999, expected_version=1)
    assert missing.value.code == "job_not_found"
    with db.get_conn(path) as conn:
        conn.execute("UPDATE research_jobs SET status = 'queued', claimed_by = NULL, lease_token = NULL, claimed_at = NULL, lease_expires_at = NULL WHERE id = ?", (running.id,))
    queued = store.get_research_job(running.id)
    with pytest.raises(ResearchJobError) as stale:
        store.cancel_research_job(queued.id, expected_version=queued.version - 1)
    assert stale.value.code == "stale_job_version"
    with pytest.raises(ResearchJobError) as secret:
        store.cancel_research_job(queued.id, expected_version=queued.version, reason_code="token=secret")
    assert secret.value.code == "invalid_cancellation_reason"
    assert "secret" not in str(secret.value)
    assert research_job_fields(store.get_research_job(queued.id)) == research_job_fields(queued)


def test_forced_cancellation_rolls_back_and_connection_recovers(tmp_path):
    path, store, running = running_job(tmp_path)
    with db.get_conn(path) as conn:
        conn.execute("UPDATE research_jobs SET status = 'queued', claimed_by = NULL, lease_token = NULL, claimed_at = NULL, lease_expires_at = NULL WHERE id = ?", (running.id,))
    queued = store.get_research_job(running.id)
    before = research_job_fields(queued)
    with db.get_conn(path) as conn:
        conn.execute("""CREATE TRIGGER force_research_job_cancellation_failure BEFORE UPDATE ON research_jobs
            WHEN OLD.status IN ('queued', 'retry_wait') AND NEW.status = 'cancelled'
            BEGIN SELECT RAISE(ABORT, 'forced_cancellation_failure'); END""")
    with pytest.raises(ResearchJobError) as error:
        store.cancel_research_job(queued.id, expected_version=queued.version)
    assert error.value.code == "cancellation_persistence_failure"
    assert "forced_cancellation_failure" not in str(error.value)
    assert research_job_fields(store.get_research_job(queued.id)) == before
    with db.get_conn(path) as conn:
        conn.execute("DROP TRIGGER force_research_job_cancellation_failure")
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    assert store.cancel_research_job(queued.id, expected_version=queued.version).status is ResearchJobStatus.CANCELLED


def test_stale_claimed_and_running_recovery_with_attempts_remaining(tmp_path):
    path, lead, *_ = setup_db(tmp_path)
    store = SqliteContactStore(path)
    first = store.enqueue_research_job(create_job(request=job_variant(lead["id"], 20).request, max_attempts=3))
    second = store.enqueue_research_job(create_job(request=job_variant(lead["id"], 21).request, max_attempts=3))
    claimed = store.claim_next_research_job(worker_id="claimed-worker")
    assert claimed is not None and claimed.id == first.id
    running = store.mark_research_job_running(claimed.id, lease_token=claimed.lease_token, expected_version=claimed.version)
    # The second job is claimed separately after making the first lease stale.
    expire_job(path, running.id)
    recovered_running = store.recover_stale_research_jobs()
    assert recovered_running[0].status is ResearchJobStatus.RETRY_WAIT
    assert recovered_running[0].safe_error_code == "lease_expired"
    assert recovered_running[0].not_before == recovered_running[0].retry_after
    assert recovered_running[0].started_at is None and recovered_running[0].completed_at is None
    assert recovered_running[0].result_summary_json is None
    assert recovered_running[0].attempt_count == running.attempt_count and recovered_running[0].version == running.version + 1
    assert all(getattr(recovered_running[0], field) is None for field in ("claimed_by", "lease_token", "claimed_at", "lease_expires_at"))
    with db.get_conn(path) as conn:
        conn.execute("UPDATE research_jobs SET not_before = NULL WHERE id = ?", (recovered_running[0].id,))
    claimed_again = store.claim_next_research_job(worker_id="claimed-worker-2")
    assert claimed_again is not None and claimed_again.id == first.id
    expire_job(path, claimed_again.id)
    recovered_claimed = store.recover_stale_research_jobs()
    assert recovered_claimed[0].status is ResearchJobStatus.RETRY_WAIT
    assert recovered_claimed[0].started_at is None
    assert second.id != first.id


@pytest.mark.parametrize("running", [False, True])
def test_exhausted_stale_claimed_and_running_jobs_become_abandoned(tmp_path, running):
    path, lead, *_ = setup_db(tmp_path)
    store = SqliteContactStore(path)
    record = store.enqueue_research_job(create_job(request=create_job().request.model_copy(update={"lead_id": lead["id"], "job_id": "abandoned"}), max_attempts=1))
    claimed = store.claim_next_research_job(worker_id="worker")
    assert claimed is not None
    target = claimed
    if running:
        target = store.mark_research_job_running(claimed.id, lease_token=claimed.lease_token, expected_version=claimed.version)
    expire_job(path, target.id, attempt_count=target.max_attempts)
    before_count = len(store.list_research_jobs())
    abandoned = store.recover_stale_research_jobs()[0]
    assert abandoned.status is ResearchJobStatus.ABANDONED
    assert abandoned.safe_error_code == "lease_expired_max_attempts"
    assert abandoned.completed_at is not None
    assert abandoned.not_before is None and abandoned.retry_after is None
    assert abandoned.attempt_count == target.attempt_count and abandoned.version == target.version + 1
    assert abandoned.started_at == (target.started_at if running else None)
    assert all(getattr(abandoned, field) is None for field in ("claimed_by", "lease_token", "claimed_at", "lease_expires_at"))
    replacement = store.enqueue_research_job(create_job(request=create_job().request.model_copy(update={"lead_id": lead["id"], "job_id": "abandoned-replacement"}), max_attempts=1))
    assert replacement.id != record.id and len(store.list_research_jobs()) == before_count + 1


def test_recovery_exclusions_remain_unchanged(tmp_path):
    path, lead, *_ = setup_db(tmp_path)
    store = SqliteContactStore(path)
    records = [store.enqueue_research_job(job_variant(lead["id"], index)) for index in range(30, 36)]
    claimed = store.claim_next_research_job(worker_id="worker")
    assert claimed is not None
    running = store.mark_research_job_running(claimed.id, lease_token=claimed.lease_token, expected_version=claimed.version)
    exclusions = [
        store.enqueue_research_job(job_variant(lead["id"], 36)),
        store.enqueue_research_job(job_variant(lead["id"], 37)),
        store.enqueue_research_job(job_variant(lead["id"], 38)),
        store.enqueue_research_job(job_variant(lead["id"], 39)),
    ]
    with db.get_conn(path) as conn:
        conn.execute("UPDATE research_jobs SET status = 'claimed', claimed_by = NULL, lease_token = NULL, claimed_at = NULL, lease_expires_at = NULL WHERE id = ?", (exclusions[0].id,))
        conn.execute("UPDATE research_jobs SET status = 'running', claimed_by = NULL, lease_token = NULL, claimed_at = NULL, lease_expires_at = NULL WHERE id = ?", (exclusions[1].id,))
        conn.execute("UPDATE research_jobs SET status = 'retry_wait', not_before = ?, retry_after = ? WHERE id = ?", ((datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(), (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(), exclusions[2].id))
        conn.execute("UPDATE research_jobs SET status = 'succeeded', completed_at = ? WHERE id = ?", (datetime.now(timezone.utc).isoformat(), exclusions[3].id))
    expire_job(path, running.id)
    expire_job(path, exclusions[0].id)
    snapshots = {record.id: research_job_fields(store.get_research_job(record.id)) for record in exclusions[1:]}
    recovered = store.recover_stale_research_jobs()
    assert {record.id for record in recovered} == {running.id, exclusions[0].id}
    for record_id, snapshot in snapshots.items():
        assert research_job_fields(store.get_research_job(record_id)) == snapshot
    queued = store.get_research_job(records[1].id)
    assert queued.status is ResearchJobStatus.QUEUED


def test_recovery_ordering_and_limit_are_directly_proven(tmp_path):
    path, lead, *_ = setup_db(tmp_path)
    store = SqliteContactStore(path)
    records = [store.enqueue_research_job(job_variant(lead["id"], index)) for index in range(50, 53)]
    claimed = []
    for worker in ("one", "two", "three"):
        item = store.claim_next_research_job(worker_id=worker)
        assert item is not None
        claimed.append(item)
    base = datetime.now(timezone.utc)
    with db.get_conn(path) as conn:
        for index, item in enumerate(claimed):
            conn.execute("UPDATE research_jobs SET lease_expires_at = ?, updated_at = ? WHERE id = ?", ((base - timedelta(minutes=3-index)).isoformat(), (base - timedelta(minutes=index)).isoformat(), item.id))
    first = store.recover_stale_research_jobs(limit=2)
    assert [item.id for item in first] == [claimed[0].id, claimed[1].id]
    remaining = store.get_research_job(claimed[2].id)
    assert remaining.status is ResearchJobStatus.CLAIMED
    second = store.recover_stale_research_jobs(limit=1)
    assert [item.id for item in second] == [claimed[2].id]
    for invalid in (True, False, 0, -1, 101, "1", 1.5):
        with pytest.raises(ResearchJobError) as error:
            store.recover_stale_research_jobs(limit=invalid)
        assert error.value.code == "invalid_recovery_limit"


def test_concurrent_recovery_returns_no_duplicate_ids_and_connections_remain_usable(tmp_path):
    path, lead, *_ = setup_db(tmp_path)
    store = SqliteContactStore(path)
    record = store.enqueue_research_job(job_variant(lead["id"], 60))
    claimed = store.claim_next_research_job(worker_id="worker")
    assert claimed is not None
    expire_job(path, claimed.id)
    def recover():
        local = SqliteContactStore(path)
        result = local.recover_stale_research_jobs()
        assert local.get_research_job(record.id).version == claimed.version + 1
        return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: recover(), range(2)))
    ids = [item.id for result in results for item in result]
    assert len(ids) == len(set(ids)) == 1
    with db.get_conn(path) as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1


def test_forced_multi_row_recovery_rolls_back_after_later_row_failure(tmp_path):
    path, lead, *_ = setup_db(tmp_path)
    store = SqliteContactStore(path)
    first = store.enqueue_research_job(job_variant(lead["id"], 70))
    second = store.enqueue_research_job(job_variant(lead["id"], 71))
    first_claim = store.claim_next_research_job(worker_id="one")
    second_claim = store.claim_next_research_job(worker_id="two")
    assert first_claim is not None and second_claim is not None
    expire_job(path, first_claim.id, expiry_offset=-3, updated_offset=-2)
    expire_job(path, second_claim.id, expiry_offset=-2, updated_offset=-1)
    before = {item.id: research_job_fields(store.get_research_job(item.id)) for item in (first_claim, second_claim)}
    with db.get_conn(path) as conn:
        conn.execute(f"""CREATE TRIGGER force_later_recovery_failure BEFORE UPDATE ON research_jobs
            WHEN OLD.id = {second_claim.id} AND NEW.status = 'retry_wait'
            BEGIN SELECT RAISE(ABORT, 'forced_later_recovery_failure'); END""")
    with pytest.raises(ResearchJobError) as error:
        store.recover_stale_research_jobs(limit=2)
    assert error.value.code == "stale_recovery_persistence_failure"
    assert "forced_later_recovery_failure" not in str(error.value)
    assert all(research_job_fields(store.get_research_job(item_id)) == snapshot for item_id, snapshot in before.items())
    with db.get_conn(path) as conn:
        conn.execute("DROP TRIGGER force_later_recovery_failure")
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    recovered = store.recover_stale_research_jobs(limit=2)
    assert {item.id for item in recovered} == {first.id, second.id}


def test_terminal_and_abandoned_active_intent_release(tmp_path):
    path, lead, *_ = setup_db(tmp_path)
    store = SqliteContactStore(path)
    terminal = store.enqueue_research_job(job_variant(lead["id"], 80))
    claimed = store.claim_next_research_job(worker_id="terminal-worker")
    assert claimed is not None
    running = store.mark_research_job_running(claimed.id, lease_token=claimed.lease_token, expected_version=claimed.version)
    finished = store.finalize_research_job(running.id, lease_token=running.lease_token, expected_version=running.version, finalization=valid_finalization(ResearchJobTerminalStatus.SUCCEEDED))
    replacement = store.enqueue_research_job(job_variant(lead["id"], 80))
    assert replacement.id != finished.id and replacement.status is ResearchJobStatus.QUEUED

    abandoned = store.enqueue_research_job(job_variant(lead["id"], 81))
    with db.get_conn(path) as conn:
        conn.execute("UPDATE research_jobs SET status = 'cancelled', completed_at = ? WHERE id = ?", (datetime.now(timezone.utc).isoformat(), replacement.id))
    abandoned_claim = store.claim_next_research_job(worker_id="abandon-worker")
    assert abandoned_claim is not None and abandoned_claim.id == abandoned.id
    expire_job(path, abandoned_claim.id, attempt_count=abandoned_claim.max_attempts)
    recovered = store.recover_stale_research_jobs()[0]
    assert recovered.id == abandoned.id and recovered.status is ResearchJobStatus.ABANDONED
    abandoned_replacement = store.enqueue_research_job(job_variant(lead["id"], 81))
    assert abandoned_replacement.id != recovered.id and abandoned_replacement.status is ResearchJobStatus.QUEUED


def test_lifecycle_operations_isolate_populated_contact_data(tmp_path):
    path, lead, *_ = setup_db(tmp_path)
    store = SqliteContactStore(path)
    tables = ("leads", "people", "tasks", "interactions", "person_candidates", "contact_method_candidates", "raw_sources")
    with db.get_conn(path) as conn:
        before = {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")] for table in tables}
    final_job = store.enqueue_research_job(job_variant(lead["id"], 90))
    retry_job = store.enqueue_research_job(job_variant(lead["id"], 91))
    cancel_job = store.enqueue_research_job(job_variant(lead["id"], 92))
    stale_job = store.enqueue_research_job(job_variant(lead["id"], 93))
    final_claim = store.claim_next_research_job(worker_id="final")
    assert final_claim is not None and final_claim.id == final_job.id
    final_running = store.mark_research_job_running(final_claim.id, lease_token=final_claim.lease_token, expected_version=final_claim.version)
    store.finalize_research_job(final_running.id, lease_token=final_running.lease_token, expected_version=final_running.version, finalization=valid_finalization(ResearchJobTerminalStatus.SUCCEEDED))
    retry_claim = store.claim_next_research_job(worker_id="retry")
    assert retry_claim is not None and retry_claim.id == retry_job.id
    retry_running = store.mark_research_job_running(retry_claim.id, lease_token=retry_claim.lease_token, expected_version=retry_claim.version)
    store.schedule_research_job_retry(retry_running.id, lease_token=retry_running.lease_token, expected_version=retry_running.version, retry=ResearchJobRetrySchedule(safe_error_code="temporary", retry_at=datetime.now(timezone.utc) + timedelta(minutes=1)))
    store.cancel_research_job(cancel_job.id, expected_version=cancel_job.version)
    stale_claim = store.claim_next_research_job(worker_id="stale")
    assert stale_claim is not None and stale_claim.id == stale_job.id
    expire_job(path, stale_claim.id)
    store.recover_stale_research_jobs()
    with db.get_conn(path) as conn:
        after = {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")] for table in tables}
    assert after == before
