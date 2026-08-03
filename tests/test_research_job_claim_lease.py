"""Focused isolated SQLite tests for E1.4.2 claim and lease ownership."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import db
import pytest

from repositories.sqlite_store import SqliteContactStore
from research_job_models import ResearchJobError, ResearchJobStatus
from tests.test_research_job_persistence import create_job, request, setup_db


NOW = datetime(2026, 8, 3, tzinfo=timezone.utc)


def enqueue(path, lead_id=1, **overrides):
    return SqliteContactStore(path).enqueue_research_job(
        create_job(request=request(lead_id=lead_id, **overrides))
    )


def set_job(path, job_id, **fields):
    assignments = ", ".join(f"{field} = ?" for field in fields)
    with db.get_conn(path) as conn:
        conn.execute(f"UPDATE research_jobs SET {assignments} WHERE id = ?", (*fields.values(), job_id))


def test_claims_due_queue_and_sets_lease_fields_exactly(tmp_path):
    path, lead, *_ = setup_db(tmp_path)
    record = enqueue(path, lead["id"])
    claimed = SqliteContactStore(path).claim_next_research_job(worker_id=" worker-a ", lease_seconds=30)
    assert claimed is not None
    assert claimed.id == record.id
    assert claimed.status is ResearchJobStatus.CLAIMED
    assert claimed.claimed_by == "worker-a"
    assert claimed.lease_token and len(claimed.lease_token) > 20
    assert claimed.claimed_at is not None and claimed.lease_expires_at is not None
    assert claimed.lease_expires_at > claimed.claimed_at
    assert claimed.attempt_count == record.attempt_count + 1
    assert claimed.version == record.version + 1
    assert claimed.started_at is None
    assert claimed.completed_at is None
    assert claimed.result_summary_json is None
    assert claimed.safe_error_code is None
    assert claimed.retry_after is None


def test_claim_eligibility_skips_future_max_attempts_owned_and_terminal_jobs(tmp_path):
    path, lead, *_ = setup_db(tmp_path)
    future = enqueue(path, lead["id"], job_id="future", requested_at=NOW)
    maxed = enqueue(path, lead["id"], job_id="maxed", target_roles=["technical_influencer"])
    owned = enqueue(path, lead["id"], job_id="owned", target_roles=["workflow_user"])
    terminal = enqueue(path, lead["id"], job_id="terminal", target_roles=["other"])
    due_retry = enqueue(path, lead["id"], job_id="retry", target_roles=["operational_owner"])
    set_job(path, future.id, not_before=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat())
    set_job(path, maxed.id, attempt_count=2)
    set_job(path, owned.id, status="claimed", claimed_by="other", lease_token="token", claimed_at=NOW.isoformat(), lease_expires_at=(datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat())
    set_job(path, terminal.id, status="succeeded")
    set_job(path, due_retry.id, status="retry_wait")
    claimed = SqliteContactStore(path).claim_next_research_job(worker_id="worker")
    assert claimed is not None and claimed.id == due_retry.id
    assert SqliteContactStore(path).claim_next_research_job(worker_id="worker") is None


def test_claim_ordering_is_priority_then_null_availability_then_time_then_id(tmp_path):
    path, lead, *_ = setup_db(tmp_path)
    low = enqueue(path, lead["id"], job_id="low", target_roles=["workflow_user"])
    null_available = enqueue(path, lead["id"], job_id="null", target_roles=["technical_influencer"])
    due_late = enqueue(path, lead["id"], job_id="late", target_roles=["operational_owner"])
    due_early = enqueue(path, lead["id"], job_id="early", target_roles=["other"])
    set_job(path, low.id, priority=10)
    set_job(path, null_available.id, priority=50, not_before=None)
    set_job(path, due_late.id, priority=50, not_before=(NOW + timedelta(seconds=30)).isoformat())
    set_job(path, due_early.id, priority=50, not_before=(NOW + timedelta(seconds=10)).isoformat())
    store = SqliteContactStore(path)
    first = store.claim_next_research_job(worker_id="worker")
    assert first is not None and first.id == null_available.id
    set_job(path, first.id, status="succeeded")
    second = store.claim_next_research_job(worker_id="worker")
    assert second is not None and second.id == due_early.id


def test_empty_queue_and_invalid_claim_inputs_are_controlled(tmp_path):
    path, *_ = setup_db(tmp_path)
    store = SqliteContactStore(path)
    assert store.claim_next_research_job(worker_id="worker") is None
    for worker in ("", "  ", "bad\nworker", "x" * 129):
        with pytest.raises(ResearchJobError) as error:
            store.claim_next_research_job(worker_id=worker)
        assert error.value.code == "invalid_worker_id"
    for duration in (0, 29, 301, -1):
        with pytest.raises(ResearchJobError) as error:
            store.claim_next_research_job(worker_id="worker", lease_seconds=duration)
        assert error.value.code == "invalid_lease_duration"


def test_two_connections_claim_one_job_and_remain_usable(tmp_path):
    path, lead, *_ = setup_db(tmp_path)
    enqueue(path, lead["id"])

    def claim(worker):
        return SqliteContactStore(path).claim_next_research_job(worker_id=worker)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ("worker-a", "worker-b")))
    claimed = [result for result in results if result is not None]
    assert len(claimed) == 1
    assert SqliteContactStore(path).list_research_jobs()
    assert sum(job.status is ResearchJobStatus.CLAIMED for job in SqliteContactStore(path).list_research_jobs()) == 1


def test_mark_running_requires_lease_state_token_version_and_expiry(tmp_path):
    path, lead, *_ = setup_db(tmp_path)
    store = SqliteContactStore(path)
    queued = enqueue(path, lead["id"])
    with pytest.raises(ResearchJobError) as queued_error:
        store.mark_research_job_running(queued.id, lease_token="wrong", expected_version=1)
    assert queued_error.value.code == "invalid_claim_state"
    claimed = store.claim_next_research_job(worker_id="worker")
    assert claimed is not None
    with pytest.raises(ResearchJobError) as token_error:
        store.mark_research_job_running(claimed.id, lease_token="wrong", expected_version=claimed.version)
    assert token_error.value.code == "lease_token_mismatch"
    with pytest.raises(ResearchJobError) as version_error:
        store.mark_research_job_running(claimed.id, lease_token=claimed.lease_token, expected_version=1)
    assert version_error.value.code == "stale_job_version"
    running = store.mark_research_job_running(claimed.id, lease_token=claimed.lease_token, expected_version=claimed.version)
    assert running.status is ResearchJobStatus.RUNNING
    assert running.started_at is not None
    assert running.attempt_count == claimed.attempt_count
    assert running.version == claimed.version + 1
    with pytest.raises(ResearchJobError) as second:
        store.mark_research_job_running(running.id, lease_token=running.lease_token, expected_version=running.version)
    assert second.value.code == "invalid_claim_state"


def test_mark_running_and_renewal_reject_expired_leases(tmp_path):
    path, lead, *_ = setup_db(tmp_path)
    store = SqliteContactStore(path)
    enqueue(path, lead["id"])
    claimed = store.claim_next_research_job(worker_id="worker")
    assert claimed is not None
    set_job(path, claimed.id, lease_expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    for operation in (
        lambda: store.mark_research_job_running(claimed.id, lease_token=claimed.lease_token, expected_version=claimed.version),
        lambda: store.renew_research_job_lease(claimed.id, lease_token=claimed.lease_token, expected_version=claimed.version),
    ):
        with pytest.raises(ResearchJobError) as error:
            operation()
        assert error.value.code == "lease_expired"


def test_renewal_requires_claimed_or_running_and_preserves_ownership(tmp_path):
    path, lead, *_ = setup_db(tmp_path)
    store = SqliteContactStore(path)
    enqueue(path, lead["id"])
    claimed = store.claim_next_research_job(worker_id="worker")
    assert claimed is not None
    renewed = store.renew_research_job_lease(
        claimed.id, lease_token=claimed.lease_token, expected_version=claimed.version, lease_seconds=300,
    )
    assert renewed.status is ResearchJobStatus.CLAIMED
    assert renewed.claimed_by == claimed.claimed_by
    assert renewed.lease_token == claimed.lease_token
    assert renewed.lease_expires_at > claimed.lease_expires_at
    assert renewed.attempt_count == claimed.attempt_count
    assert renewed.version == claimed.version + 1
    running = store.mark_research_job_running(renewed.id, lease_token=renewed.lease_token, expected_version=renewed.version)
    renewed_running = store.renew_research_job_lease(running.id, lease_token=running.lease_token, expected_version=running.version)
    assert renewed_running.status is ResearchJobStatus.RUNNING
    assert renewed_running.started_at == running.started_at
    for duration in (29, 301):
        with pytest.raises(ResearchJobError) as error:
            store.renew_research_job_lease(renewed_running.id, lease_token=renewed_running.lease_token, expected_version=renewed_running.version, lease_seconds=duration)
        assert error.value.code == "invalid_lease_duration"


def test_failed_claim_update_rolls_back_and_preserves_seeded_data(tmp_path):
    path, lead, *_ = setup_db(tmp_path)
    store = SqliteContactStore(path)
    original = enqueue(path, lead["id"])
    with db.get_conn(path) as conn:
        conn.execute(
            """CREATE TRIGGER fail_claim_update BEFORE UPDATE OF status ON research_jobs
               BEGIN SELECT RAISE(ABORT, 'forced claim failure'); END"""
        )
    with pytest.raises(ResearchJobError) as error:
        store.claim_next_research_job(worker_id="worker")
    assert error.value.code == "claim_persistence_failure"
    assert "forced claim failure" not in str(error.value)
    current = store.get_research_job(original.id)
    assert current.status is ResearchJobStatus.QUEUED
    assert current.attempt_count == 0 and current.version == 1
    with db.get_conn(path) as conn:
        conn.execute("DROP TRIGGER fail_claim_update")
    assert store.get_research_job(original.id).status is ResearchJobStatus.QUEUED


def test_claim_running_renewal_do_not_mutate_canonical_or_candidate_rows(tmp_path):
    path, lead, *_ = setup_db(tmp_path)
    store = SqliteContactStore(path)
    tables = ("leads", "people", "tasks", "interactions", "person_candidates", "contact_method_candidates")
    with db.get_conn(path) as conn:
        before = {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")] for table in tables}
    enqueue(path, lead["id"])
    claimed = store.claim_next_research_job(worker_id="worker")
    assert claimed is not None
    running = store.mark_research_job_running(claimed.id, lease_token=claimed.lease_token, expected_version=claimed.version)
    store.renew_research_job_lease(running.id, lease_token=running.lease_token, expected_version=running.version)
    with db.get_conn(path) as conn:
        after = {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")] for table in tables}
        assert conn.execute("SELECT COUNT(*) AS count FROM raw_sources").fetchone()["count"] == 1
    assert after == before


def test_mark_running_forced_failure_rolls_back_every_job_field(tmp_path):
    path, lead, *_ = setup_db(tmp_path)
    store = SqliteContactStore(path)
    enqueue(path, lead["id"])
    claimed = store.claim_next_research_job(worker_id="worker")
    assert claimed is not None
    before = store.get_research_job(claimed.id)
    with db.get_conn(path) as conn:
        conn.execute(
            """CREATE TRIGGER force_running_update_failure
               BEFORE UPDATE ON research_jobs
               WHEN OLD.status = 'claimed' AND NEW.status = 'running'
               BEGIN SELECT RAISE(ABORT, 'forced_running_update_failure'); END"""
        )
    with pytest.raises(ResearchJobError) as error:
        store.mark_research_job_running(
            claimed.id, lease_token=claimed.lease_token, expected_version=claimed.version,
        )
    assert error.value.code == "lease_update_persistence_failure"
    assert "forced_running_update_failure" not in str(error.value)
    assert claimed.lease_token not in str(error.value)
    assert store.get_research_job(claimed.id) == before
    with db.get_conn(path) as conn:
        conn.execute("DROP TRIGGER force_running_update_failure")
    running = store.mark_research_job_running(
        claimed.id, lease_token=claimed.lease_token, expected_version=claimed.version,
    )
    assert running.status is ResearchJobStatus.RUNNING


def test_renewal_forced_failure_rolls_back_every_job_field(tmp_path):
    path, lead, *_ = setup_db(tmp_path)
    store = SqliteContactStore(path)
    enqueue(path, lead["id"])
    claimed = store.claim_next_research_job(worker_id="worker")
    assert claimed is not None
    before = store.get_research_job(claimed.id)
    with db.get_conn(path) as conn:
        conn.execute(
            """CREATE TRIGGER force_lease_renewal_failure
               BEFORE UPDATE ON research_jobs
               WHEN OLD.lease_expires_at IS NOT NULL
                AND NEW.lease_expires_at <> OLD.lease_expires_at
                AND NEW.status = OLD.status
               BEGIN SELECT RAISE(ABORT, 'forced_lease_renewal_failure'); END"""
        )
    with pytest.raises(ResearchJobError) as error:
        store.renew_research_job_lease(
            claimed.id, lease_token=claimed.lease_token, expected_version=claimed.version,
        )
    assert error.value.code == "lease_update_persistence_failure"
    assert "forced_lease_renewal_failure" not in str(error.value)
    assert claimed.lease_token not in str(error.value)
    assert store.get_research_job(claimed.id) == before
    with db.get_conn(path) as conn:
        conn.execute("DROP TRIGGER force_lease_renewal_failure")
    renewed = store.renew_research_job_lease(
        claimed.id, lease_token=claimed.lease_token, expected_version=claimed.version,
    )
    assert renewed.version == claimed.version + 1


@pytest.mark.parametrize(
    "status",
    [
        "queued", "retry_wait", "running", "succeeded", "partial", "no_result",
        "needs_review", "failed", "cancelled", "abandoned",
    ],
)
def test_mark_running_rejects_every_invalid_state(tmp_path, status):
    path, lead, *_ = setup_db(tmp_path)
    store = SqliteContactStore(path)
    record = enqueue(path, lead["id"])
    set_job(path, record.id, status=status)
    with pytest.raises(ResearchJobError) as error:
        store.mark_research_job_running(record.id, lease_token="wrong", expected_version=1)
    assert error.value.code == "invalid_claim_state"


def test_mark_running_rejects_missing_job_and_precedence_is_deterministic(tmp_path):
    path, lead, *_ = setup_db(tmp_path)
    store = SqliteContactStore(path)
    with pytest.raises(ResearchJobError) as missing:
        store.mark_research_job_running(99999, lease_token="wrong", expected_version=99)
    assert missing.value.code == "job_not_found"
    enqueue(path, lead["id"])
    claimed = store.claim_next_research_job(worker_id="worker")
    assert claimed is not None
    with pytest.raises(ResearchJobError) as stale:
        store.mark_research_job_running(claimed.id, lease_token="wrong", expected_version=claimed.version - 1)
    assert stale.value.code == "stale_job_version"


@pytest.mark.parametrize("status", ["queued", "retry_wait", "succeeded", "partial", "no_result", "needs_review", "failed", "cancelled", "abandoned"])
def test_renewal_rejects_unowned_and_terminal_states(tmp_path, status):
    path, lead, *_ = setup_db(tmp_path)
    store = SqliteContactStore(path)
    record = enqueue(path, lead["id"])
    set_job(path, record.id, status=status)
    with pytest.raises(ResearchJobError) as error:
        store.renew_research_job_lease(record.id, lease_token="wrong", expected_version=1)
    assert error.value.code == "invalid_claim_state"


def test_renewal_rejects_missing_job_wrong_token_stale_version_and_bad_duration(tmp_path):
    path, lead, *_ = setup_db(tmp_path)
    store = SqliteContactStore(path)
    with pytest.raises(ResearchJobError) as missing:
        store.renew_research_job_lease(99999, lease_token="wrong", expected_version=1)
    assert missing.value.code == "job_not_found"
    enqueue(path, lead["id"])
    claimed = store.claim_next_research_job(worker_id="worker")
    assert claimed is not None
    with pytest.raises(ResearchJobError) as token:
        store.renew_research_job_lease(claimed.id, lease_token="wrong", expected_version=claimed.version)
    assert token.value.code == "lease_token_mismatch"
    with pytest.raises(ResearchJobError) as stale:
        store.renew_research_job_lease(claimed.id, lease_token=claimed.lease_token, expected_version=claimed.version - 1)
    assert stale.value.code == "stale_job_version"
    for duration in (29, 301, 30.5, True):
        with pytest.raises(ResearchJobError) as invalid:
            store.renew_research_job_lease(
                claimed.id, lease_token=claimed.lease_token, expected_version=claimed.version,
                lease_seconds=duration,
            )
        assert invalid.value.code == "invalid_lease_duration"


def test_rejected_guards_preserve_job_state_and_expired_lease_is_classified(tmp_path):
    path, lead, *_ = setup_db(tmp_path)
    store = SqliteContactStore(path)
    enqueue(path, lead["id"])
    claimed = store.claim_next_research_job(worker_id="worker")
    assert claimed is not None
    before = store.get_research_job(claimed.id)
    with pytest.raises(ResearchJobError):
        store.mark_research_job_running(claimed.id, lease_token="wrong", expected_version=claimed.version)
    assert store.get_research_job(claimed.id) == before
    with pytest.raises(ResearchJobError):
        store.renew_research_job_lease(claimed.id, lease_token=claimed.lease_token, expected_version=claimed.version - 1)
    assert store.get_research_job(claimed.id) == before
    set_job(path, claimed.id, lease_expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    expired = store.get_research_job(claimed.id)
    with pytest.raises(ResearchJobError) as error:
        store.renew_research_job_lease(claimed.id, lease_token=expired.lease_token, expected_version=expired.version)
    assert error.value.code == "lease_expired"
    assert store.get_research_job(claimed.id) == expired
