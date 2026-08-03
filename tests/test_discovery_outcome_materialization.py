from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import db
import pytest

from discovery_models import DiscoveryContact, DiscoveryOutcomeStatus
from discovery_materialization_models import DiscoveryOutcomeMaterializationResult
from repositories.sqlite_store import SqliteContactStore
from research_job_models import ResearchJobError, ResearchJobFinalization, ResearchJobResultSummary, ResearchJobStatus
from services.discovery_outcome_materialization import materialize_discovery_outcome
from tests.test_discovery_contracts import candidate as make_candidate, outcome, source
from tests.test_research_job_lifecycle import expire_job, running_job
from tests.test_research_job_persistence import create_job, setup_db


def _materialize(path, store, running, value=None):
    return materialize_discovery_outcome(
        store,
        research_job_id=running.id,
        lease_token=running.lease_token,
        expected_version=running.version,
        outcome=value or outcome(),
    )


def _counts(path):
    with db.get_conn(path) as conn:
        return {
            table: conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
            for table in (
                "raw_sources", "person_candidates", "contact_method_candidates",
                "research_job_materializations", "research_job_materialization_sources",
                "research_job_materialization_person_candidates", "research_job_materialization_contact_candidates",
            )
        }


def _running_job_with_result_limit(tmp_path, result_limit: int):
    path, lead, *_ = setup_db(tmp_path)
    store = SqliteContactStore(path)
    request = create_job().request.model_copy(update={"lead_id": lead["id"], "result_limit": result_limit})
    job = store.enqueue_research_job(create_job(request=request))
    claimed = store.claim_next_research_job(worker_id="bound-worker")
    assert claimed is not None
    return path, store, store.mark_research_job_running(
        claimed.id, lease_token=claimed.lease_token, expected_version=claimed.version,
    )


def _two_source_outcome():
    first_source = source()
    second_source = first_source.model_copy(update={
        "source_url": "https://example.com/team",
        "canonical_url": "https://example.com/team",
        "content_hash": "b" * 64,
        "extracted_text": "Team evidence",
        "provider_request_id": "provider-2",
    })
    first_candidate = make_candidate()
    second_candidate = make_candidate().model_copy(update={
        "name": "John Smith",
        "normalized_name": "john smith",
        "source_url": second_source.source_url,
    })
    return outcome(
        sources=[first_source, second_source],
        candidates=[first_candidate, second_candidate],
    )


def _contact_outcome():
    contact = DiscoveryContact(
        kind="email", value="jane@example.com", normalized_value="jane@example.com",
        evidence_basis="source_confirmed", verification_status="source_confirmed",
        source_url="https://example.com/about",
    )
    return outcome(candidates=[make_candidate().model_copy(update={"explicit_contacts": [contact]})])


def test_schema_is_repeatable_and_uses_receipt_links_only(tmp_path):
    path, _, _ = running_job(tmp_path)
    db.init_db(path)
    with db.get_conn(path) as conn:
        names = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "research_job_materializations" in names
        assert "research_job_materialization_sources" in names
        assert "research_job_materialization_person_candidates" in names
        assert "research_job_materialization_contact_candidates" in names
        assert not {"discovery_sources", "discovery_candidates", "discovery_people", "discovery_contacts"} & names
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(research_job_materializations)")}
        assert "lease_token" not in cols and "outcome_json" not in cols
        unique = conn.execute("PRAGMA index_list(research_job_materializations)").fetchall()
        assert any(row["unique"] for row in unique)


@pytest.mark.parametrize("status", [
    DiscoveryOutcomeStatus.SUCCEEDED,
    DiscoveryOutcomeStatus.PARTIAL,
    DiscoveryOutcomeStatus.NO_RESULT,
    DiscoveryOutcomeStatus.NEEDS_REVIEW,
])
def test_materializable_statuses_create_typed_receipts(tmp_path, status):
    path, store, running = running_job(tmp_path)
    value = outcome(
        status,
        candidates=[] if status in {DiscoveryOutcomeStatus.NO_RESULT, DiscoveryOutcomeStatus.NEEDS_REVIEW} else [make_candidate()],
        no_result_reason="no_result" if status is DiscoveryOutcomeStatus.NO_RESULT else None,
    )
    result = _materialize(path, store, running, value)
    assert isinstance(result, DiscoveryOutcomeMaterializationResult)
    assert result.outcome_status is status
    assert result.replayed is False
    assert store.get_research_job(running.id).model_dump(mode="json") == running.model_dump(mode="json")


@pytest.mark.parametrize("status", [
    DiscoveryOutcomeStatus.RATE_LIMITED,
    DiscoveryOutcomeStatus.RETRYABLE_ERROR,
    DiscoveryOutcomeStatus.PERMANENT_ERROR,
    DiscoveryOutcomeStatus.CANCELLED,
])
def test_control_outcomes_are_rejected_without_rows(tmp_path, status):
    path, store, running = running_job(tmp_path)
    kwargs = {"sources": [], "candidates": []}
    if status is DiscoveryOutcomeStatus.RATE_LIMITED:
        kwargs["retry_after"] = datetime.now(timezone.utc) + timedelta(minutes=1)
    if status is DiscoveryOutcomeStatus.RETRYABLE_ERROR:
        kwargs["safe_error_code"] = "temporary"
    if status is DiscoveryOutcomeStatus.PERMANENT_ERROR:
        kwargs["safe_error_code"] = "permanent"
    with pytest.raises(ResearchJobError) as error:
        _materialize(path, store, running, outcome(status, **kwargs))
    assert error.value.code == "outcome_not_materializable"
    assert _counts(path)["research_job_materializations"] == 0


def test_running_guards_and_request_bounds_fail_closed(tmp_path):
    path, store, running = _running_job_with_result_limit(tmp_path, 1)
    for kwargs, code in (
        ({"lease_token": "wrong"}, "lease_token_mismatch"),
        ({"expected_version": running.version - 1}, "stale_job_version"),
    ):
        call = {"lease_token": running.lease_token, "expected_version": running.version}
        call.update(kwargs)
        with pytest.raises(ResearchJobError) as error:
            materialize_discovery_outcome(store, research_job_id=running.id, outcome=outcome(), **call)
        assert error.value.code == code
    too_many = _two_source_outcome()
    before_counts = _counts(path)
    with pytest.raises(ResearchJobError) as error:
        _materialize(path, store, running, too_many)
    assert error.value.code == "outcome_exceeds_request_bounds"
    assert _counts(path) == before_counts
    assert store.get_research_job(running.id).model_dump(mode="json") == running.model_dump(mode="json")


def test_expired_zero_attempt_and_nonrunning_jobs_are_rejected(tmp_path):
    path, store, running = running_job(tmp_path)
    expire_job(path, running.id)
    with pytest.raises(ResearchJobError) as error:
        _materialize(path, store, store.get_research_job(running.id))
    assert error.value.code == "lease_expired"

    second = tmp_path / "second"
    second.mkdir()
    path, store, running = running_job(second)
    with db.get_conn(path) as conn:
        conn.execute("UPDATE research_jobs SET attempt_count = 0 WHERE id = ?", (running.id,))
    fresh = store.get_research_job(running.id)
    with pytest.raises(ResearchJobError) as error:
        _materialize(path, store, fresh)
    assert error.value.code == "invalid_attempt_count"


def test_sources_candidates_contacts_and_evidence_are_materialized(tmp_path):
    path, store, running = running_job(tmp_path)
    value = outcome()
    result = _materialize(path, store, running, value)
    assert len(result.raw_source_ids) == 1
    assert len(result.person_candidate_ids) == 1
    assert result.contact_candidate_ids == ()
    with db.get_conn(path) as conn:
        raw = conn.execute("SELECT * FROM raw_sources WHERE id = ?", (result.raw_source_ids[0],)).fetchone()
        metadata = json.loads(raw["parsed_json"])
        assert raw["lead_id"] == running.lead_id
        assert raw["source_url"] == value.sources[0].source_url
        assert metadata["content_hash"] == value.sources[0].content_hash
        person = conn.execute("SELECT * FROM person_candidates WHERE id = ?", (result.person_candidate_ids[0],)).fetchone()
        assert person["raw_source_id"] == raw["id"]


def test_explicit_contact_evidence_is_persisted_without_guessing(tmp_path):
    path, store, running = running_job(tmp_path)
    value = _contact_outcome()
    result = _materialize(path, store, running, value)
    assert len(result.contact_candidate_ids) == 1
    with db.get_conn(path) as conn:
        row = conn.execute("SELECT * FROM contact_method_candidates WHERE id = ?", (result.contact_candidate_ids[0],)).fetchone()
        assert row["person_candidate_id"] == result.person_candidate_ids[0]
        assert row["normalized_value"] == "jane@example.com"


def test_unresolved_candidate_evidence_fails_and_rolls_back(tmp_path):
    path, store, running = running_job(tmp_path)
    before = _counts(path)
    invalid = make_candidate().model_copy(update={"source_url": "https://not-in-outcome.example/person"})
    with pytest.raises(ResearchJobError) as error:
        _materialize(path, store, running, outcome(candidates=[invalid]))
    assert error.value.code == "candidate_source_unresolved"
    assert _counts(path) == before
    assert store.get_research_job(running.id).status is ResearchJobStatus.RUNNING


def test_exact_replay_is_idempotent_and_digest_is_deterministic(tmp_path):
    path, store, running = running_job(tmp_path)
    first = _materialize(path, store, running)
    counts = _counts(path)
    replay = _materialize(path, store, running)
    assert replay.replayed is True
    assert replay == first.model_copy(update={"replayed": True})
    assert _counts(path) == counts
    assert len(first.outcome_digest) == 64


def test_same_attempt_changed_outcome_conflicts_without_overwrite(tmp_path):
    path, store, running = running_job(tmp_path)
    first = _materialize(path, store, running)
    changed = outcome(warnings=["different_warning"])
    with pytest.raises(ResearchJobError) as error:
        _materialize(path, store, running, changed)
    assert error.value.code == "materialization_conflict"
    assert _counts(path)["research_job_materializations"] == 1
    with db.get_conn(path) as conn:
        digest = conn.execute("SELECT outcome_digest FROM research_job_materializations WHERE id = ?", (first.materialization_id,)).fetchone()[0]
        assert digest == first.outcome_digest


def test_new_attempt_reuses_domain_rows_and_creates_new_receipt_links(tmp_path):
    path, store, running = running_job(tmp_path)
    value = _contact_outcome()
    first = _materialize(path, store, running, value)
    with db.get_conn(path) as conn:
        first_receipt = dict(conn.execute(
            "SELECT * FROM research_job_materializations WHERE id = ?", (first.materialization_id,)
        ).fetchone())
    expire_job(path, running.id)
    recovered = store.recover_stale_research_jobs()
    assert recovered and recovered[0].status is ResearchJobStatus.RETRY_WAIT
    claimed = store.claim_next_research_job(worker_id="second-worker")
    assert claimed is not None and claimed.attempt_count == running.attempt_count + 1
    running_again = store.mark_research_job_running(
        claimed.id, lease_token=claimed.lease_token, expected_version=claimed.version,
    )
    second = _materialize(path, store, running_again, value)
    assert second.materialization_id != first.materialization_id
    assert second.attempt_count != first.attempt_count
    assert second.outcome_digest == first.outcome_digest
    assert second.raw_source_ids == first.raw_source_ids
    assert second.person_candidate_ids == first.person_candidate_ids
    assert second.contact_candidate_ids == first.contact_candidate_ids
    with db.get_conn(path) as conn:
        receipts = conn.execute(
            "SELECT * FROM research_job_materializations WHERE research_job_id = ? ORDER BY attempt_count",
            (running.id,),
        ).fetchall()
        assert len(receipts) == 2
        assert dict(receipts[0]) == first_receipt
        assert conn.execute("SELECT COUNT(*) FROM raw_sources WHERE lead_id = ?", (running.lead_id,)).fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM person_candidates WHERE lead_id = ?", (running.lead_id,)).fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM contact_method_candidates WHERE lead_id = ?", (running.lead_id,)).fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM research_job_materialization_sources").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM research_job_materialization_person_candidates").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM research_job_materialization_contact_candidates").fetchone()[0] == 2
    assert store.get_research_job(running.id).status is ResearchJobStatus.RUNNING


def test_changed_content_hash_creates_immutable_source_snapshot(tmp_path):
    path, store, running = running_job(tmp_path)
    before = _counts(path)
    first = _materialize(path, store, running)
    value = outcome()
    changed_source = value.sources[0].model_copy(update={"content_hash": "a" * 64, "extracted_text": "changed"})
    changed_candidate = value.candidates[0].model_copy(update={"source_url": changed_source.source_url})
    changed = value.model_copy(update={"sources": [changed_source], "candidates": [changed_candidate], "warnings": ["new"]})
    with pytest.raises(ResearchJobError):
        _materialize(path, store, running, changed)
    assert _counts(path)["raw_sources"] == before["raw_sources"] + 1
    assert first.raw_source_ids[0] != 0


def test_forced_source_failure_rolls_back_everything(tmp_path, monkeypatch):
    path, store, running = running_job(tmp_path)
    before = _counts(path)
    original = db.create_or_reuse_person_candidate

    def fail(*args, **kwargs):
        raise sqlite3.IntegrityError("private sqlite detail")

    monkeypatch.setattr(db, "create_or_reuse_person_candidate", fail)
    with pytest.raises(ResearchJobError) as error:
        _materialize(path, store, running)
    assert error.value.code == "candidate_persistence_failure"
    assert _counts(path) == before
    assert store.get_research_job(running.id).status is ResearchJobStatus.RUNNING
    monkeypatch.setattr(db, "create_or_reuse_person_candidate", original)
    with db.get_conn(path) as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1


def test_forced_receipt_failure_rolls_back_sources_and_candidates(tmp_path, monkeypatch):
    path, store, running = running_job(tmp_path)
    before = _counts(path)
    with db.get_conn(path) as conn:
        conn.execute("CREATE TRIGGER fail_materialization_receipt BEFORE INSERT ON research_job_materializations BEGIN SELECT RAISE(ABORT, 'private sqlite detail'); END")
    with pytest.raises(ResearchJobError) as error:
        _materialize(path, store, running)
    assert error.value.code == "materialization_persistence_failure"
    assert _counts(path) == before
    with db.get_conn(path) as conn:
        conn.execute("DROP TRIGGER fail_materialization_receipt")


def test_later_candidate_failure_rolls_back_earlier_rows_and_later_success_works(tmp_path, monkeypatch):
    path, store, running = running_job(tmp_path)
    value = _two_source_outcome()
    before_counts = _counts(path)
    before_job = store.get_research_job(running.id)
    original = db.create_or_reuse_person_candidate
    calls = {"count": 0}

    def fail_second(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise sqlite3.IntegrityError("private trigger and lease_token=opaque")
        return original(*args, **kwargs)

    monkeypatch.setattr(db, "create_or_reuse_person_candidate", fail_second)
    with pytest.raises(ResearchJobError) as error:
        _materialize(path, store, running, value)
    assert error.value.code == "candidate_persistence_failure"
    assert "private trigger" not in str(error.value)
    assert "lease_token" not in str(error.value)
    assert _counts(path) == before_counts
    assert store.get_research_job(running.id).model_dump(mode="json") == before_job.model_dump(mode="json")
    with db.get_conn(path) as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    monkeypatch.undo()
    success = _materialize(path, store, running, value)
    assert success.replayed is False
    assert len(success.person_candidate_ids) == 2


def test_concurrent_materialization_returns_one_receipt_and_same_ids(tmp_path):
    path, store, running = running_job(tmp_path)
    value = outcome()

    def run():
        local = SqliteContactStore(path)
        return _materialize(path, local, local.get_research_job(running.id), value)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert {result.materialization_id for result in results} == {results[0].materialization_id}
    assert {result.raw_source_ids for result in results} == {results[0].raw_source_ids}
    assert sorted(result.replayed for result in results) == [False, True]
    assert _counts(path)["research_job_materializations"] == 1
    with db.get_conn(path) as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1


def test_materialization_does_not_create_canonical_or_related_rows(tmp_path):
    path, store, running = running_job(tmp_path)
    with db.get_conn(path) as conn:
        before = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("people", "tasks", "interactions")}
    _materialize(path, store, running)
    with db.get_conn(path) as conn:
        after = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before}
    assert after == before


def test_unrelated_person_and_contact_candidates_are_fieldwise_isolated(tmp_path):
    path, lead, other, *_ = setup_db(tmp_path)
    store = SqliteContactStore(path)
    other_source = db.create_raw_source("note", "unrelated source", lead_id=other["id"], db_path=path)
    other_person = db.create_or_reuse_person_candidate(
        other["id"], other_source["id"], name="Unrelated Person", title="Owner",
        role_type="economic_buyer", source_type="note", discovery_method="manual_paste",
        confidence=0.61, db_path=path,
    )
    other_contact = db.create_or_reuse_contact_candidate(
        other["id"], other_source["id"], person_candidate_id=other_person["id"], kind="email",
        value="unrelated@example.com", discovery_method="manual_paste", db_path=path,
    )
    job = store.enqueue_research_job(create_job(request=create_job().request.model_copy(update={"lead_id": lead["id"]})))
    claimed = store.claim_next_research_job(worker_id="isolation-worker")
    assert claimed is not None
    running = store.mark_research_job_running(claimed.id, lease_token=claimed.lease_token, expected_version=claimed.version)

    def snapshots():
        with db.get_conn(path) as conn:
            return (
                dict(conn.execute("SELECT * FROM person_candidates WHERE id = ?", (other_person["id"],)).fetchone()),
                dict(conn.execute("SELECT * FROM contact_method_candidates WHERE id = ?", (other_contact["id"],)).fetchone()),
            )

    baseline = snapshots()
    first = _materialize(path, store, running)
    assert snapshots() == baseline
    replay = _materialize(path, store, running)
    assert replay.replayed is True
    assert snapshots() == baseline
    with pytest.raises(ResearchJobError) as conflict:
        _materialize(path, store, running, outcome(warnings=["conflict"]))
    assert conflict.value.code == "materialization_conflict"
    assert snapshots() == baseline

    store.finalize_research_job(
        running.id, lease_token=running.lease_token, expected_version=running.version,
        finalization=ResearchJobFinalization(
            status="succeeded", summary=ResearchJobResultSummary(source_count=1, candidate_count=1),
        ),
    )
    second_job = store.enqueue_research_job(create_job(request=create_job().request.model_copy(update={
        "lead_id": lead["id"], "job_id": "isolation-second",
    })))
    second_claim = store.claim_next_research_job(worker_id="isolation-second-worker")
    assert second_claim is not None and second_claim.id == second_job.id
    second_running = store.mark_research_job_running(
        second_claim.id, lease_token=second_claim.lease_token, expected_version=second_claim.version,
    )
    with db.get_conn(path) as conn:
        conn.execute("CREATE TRIGGER fail_isolation_receipt BEFORE INSERT ON research_job_materializations BEGIN SELECT RAISE(ABORT, 'unrelated trigger'); END")
    with pytest.raises(ResearchJobError):
        _materialize(path, store, second_running)
    with db.get_conn(path) as conn:
        conn.execute("DROP TRIGGER fail_isolation_receipt")
    assert snapshots() == baseline
