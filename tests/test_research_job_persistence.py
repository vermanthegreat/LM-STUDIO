"""Focused isolated SQLite tests for the E1.4.1 research-job foundation."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import db
import pytest
from pydantic import ValidationError

from discovery_models import DiscoveryRequest
from repositories.sqlite_store import SqliteContactStore
from research_job_models import (
    ADAPTER_KEYS,
    ResearchJobCreate,
    ResearchJobError,
    ResearchJobStatus,
    canonical_request_json,
    research_intent_key,
)


NOW = datetime(2026, 8, 3, tzinfo=timezone.utc)


def request(**overrides) -> DiscoveryRequest:
    data = {
        "job_id": "request-1",
        "lead_id": 1,
        "company_name": "Example Agency",
        "normalized_domain": "example.com",
        "company_website": "https://example.com/",
        "target_roles": ["workflow_user", "economic_buyer", "economic_buyer"],
        "result_limit": 2,
        "approved_source_types": ["website", "note", "website"],
        "requested_at": NOW,
        "requester_identity": "operator-a",
        "correlation_id": "correlation-1",
        "provider_config_ref": "fake-default",
        "max_pages": 2,
        "max_requests": 3,
        "timeout_seconds": 10,
    }
    data.update(overrides)
    return DiscoveryRequest(**data)


def create_job(**overrides) -> ResearchJobCreate:
    data = {"request": request(), "adapter_key": "fake", "priority": 50, "max_attempts": 2}
    data.update(overrides)
    return ResearchJobCreate(**data)


def setup_db(tmp_path):
    path = tmp_path / "research-jobs.db"
    db.init_db(path)
    lead, _ = db.upsert_lead(
        {
            "company_name": "Example Agency",
            "company_email": "company@example.com",
            "company_phone": "+1 555 0100",
            "website": "https://example.com/",
            "enrichment_status": "pending",
        },
        db_path=path,
    )
    other, _ = db.upsert_lead({"company_name": "Other Agency", "website": "https://other.example"}, db_path=path)
    source = db.create_raw_source("note", "seed source", lead_id=lead["id"], db_path=path)
    person = db.add_person(lead["id"], {"name": "Jane Doe", "title": "Founder"}, db_path=path)
    db.add_interaction(lead["id"], {"type": "note", "summary": "seed interaction"}, db_path=path)
    db.add_task(lead["id"], {"title": "seed task"}, db_path=path)
    person_candidate = db.create_or_reuse_person_candidate(
        lead["id"], source["id"], name="Candidate Jane", title="Founder", role_type="economic_buyer",
        source_type="note", discovery_method="manual_paste", confidence=0.8, db_path=path,
    )
    contact_candidate = db.create_or_reuse_contact_candidate(
        lead["id"], source["id"], person_candidate_id=person_candidate["id"], kind="email",
        value="candidate@example.com", discovery_method="manual_paste", db_path=path,
    )
    return path, lead, other, person, person_candidate, contact_candidate


def test_schema_is_additive_repeatable_and_has_only_one_job_table(tmp_path):
    path, *_ = setup_db(tmp_path)
    db.init_db(path)
    with db.get_conn(path) as conn:
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        indexes = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(research_jobs)")}
        active_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'idx_research_jobs_active_intent'"
        ).fetchone()["sql"]
    assert "research_jobs" in tables
    assert "discovery_jobs" not in tables
    assert {"idx_research_jobs_active_intent", "idx_research_jobs_lead_created", "idx_research_jobs_queue"} <= indexes
    assert {"id", "lead_id", "intent_key", "request_snapshot_json", "status", "version", "updated_at"} <= columns
    assert all(state in active_sql for state in ("queued", "claimed", "running", "retry_wait"))


def test_enqueue_validates_fake_request_and_round_trips_canonical_snapshot(tmp_path):
    path, lead, *_ = setup_db(tmp_path)
    job = create_job(request=request(lead_id=lead["id"]))
    store = SqliteContactStore(path)
    record = store.enqueue_research_job(job)
    assert record.status is ResearchJobStatus.QUEUED
    assert record.attempt_count == 0
    assert record.version == 1
    assert record.target_roles == ("economic_buyer", "workflow_user")
    assert record.approved_source_types == ("note", "website")
    assert json.loads(record.request_snapshot_json) == json.loads(canonical_request_json(job.request))
    assert record.request_snapshot == job.request
    assert all(getattr(record, field) is None for field in (
        "claimed_by", "lease_token", "claimed_at", "lease_expires_at", "started_at",
        "completed_at", "result_summary_json", "safe_error_code", "retry_after",
    ))


def test_company_website_adapter_key_is_exact_and_deterministic():
    assert ADAPTER_KEYS == ("fake", "company_website")
    fake = create_job(adapter_key="fake")
    company_website = create_job(adapter_key="company_website")
    repeated = create_job(adapter_key="company_website")
    assert company_website.adapter_key == "company_website"
    assert canonical_request_json(company_website.request) == canonical_request_json(repeated.request)
    assert research_intent_key(company_website) == research_intent_key(repeated)
    assert research_intent_key(fake) != research_intent_key(company_website)


@pytest.mark.parametrize("adapter_key", [
    "",
    " ",
    "browser",
    "COMPANY_WEBSITE",
    "company_website ",
    "companywebsite",
    "company_website_v1",
    "company_website_secret-shaped-value",
])
def test_company_website_adapter_key_rejects_unknown_and_malformed_values(adapter_key):
    with pytest.raises(ValidationError) as error:
        create_job(adapter_key=adapter_key)
    if "secret-shaped" in adapter_key:
        assert adapter_key not in str(error.value)


def test_enqueue_rejects_missing_lead_adapter_attempts_priority_and_dict_request(tmp_path):
    path, *_ = setup_db(tmp_path)
    store = SqliteContactStore(path)
    with pytest.raises(ResearchJobError) as missing:
        store.enqueue_research_job(create_job(request=request(lead_id=999)))
    assert missing.value.code == "lead_not_found"
    with pytest.raises(ValidationError):
        create_job(adapter_key="browser")
    with pytest.raises(ValidationError):
        create_job(max_attempts=4)
    with pytest.raises(ValidationError):
        create_job(priority=101)
    with pytest.raises(ValidationError, match="validated DiscoveryRequest"):
        ResearchJobCreate(request=request().model_dump(), adapter_key="fake", priority=1, max_attempts=1)


def test_active_equivalent_enqueue_deduplicates_without_overwriting_first_request(tmp_path):
    path, lead, *_ = setup_db(tmp_path)
    first = create_job(request=request(lead_id=lead["id"]))
    replay = create_job(
        request=request(
            lead_id=lead["id"], job_id="request-2", requested_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
            requester_identity="operator-b", correlation_id="correlation-2",
        ),
        priority=1,
    )
    store = SqliteContactStore(path)
    original = store.enqueue_research_job(first)
    returned = store.enqueue_research_job(replay)
    assert returned.id == original.id
    assert returned.request_snapshot_json == original.request_snapshot_json
    assert returned.requested_by == original.requested_by
    assert returned.correlation_id == original.correlation_id
    assert returned.priority == original.priority
    assert store.list_research_jobs() == [original]


def test_different_intents_and_terminal_prior_job_create_new_rows(tmp_path):
    path, lead, other, *_ = setup_db(tmp_path)
    store = SqliteContactStore(path)
    first = store.enqueue_research_job(create_job(request=request(lead_id=lead["id"])))
    variants = [
        create_job(request=request(lead_id=other["id"], job_id="other")),
        create_job(request=request(lead_id=lead["id"], job_id="roles", target_roles=["technical_influencer"])),
        create_job(request=request(lead_id=lead["id"], job_id="sources", approved_source_types=["linkedin_company"])),
        create_job(request=request(lead_id=lead["id"], job_id="site", company_website="https://other.example", normalized_domain="other.example")),
    ]
    ids = {store.enqueue_research_job(variant).id for variant in variants}
    assert len(ids) == 4
    with db.get_conn(path) as conn:
        conn.execute("UPDATE research_jobs SET status = 'succeeded' WHERE id = ?", (first.id,))
    terminal_replay = store.enqueue_research_job(create_job(request=request(lead_id=lead["id"], job_id="new-after-terminal")))
    assert terminal_replay.id != first.id
    assert terminal_replay.status is ResearchJobStatus.QUEUED


def test_concurrent_equivalent_enqueue_returns_one_winning_row(tmp_path):
    path, lead, *_ = setup_db(tmp_path)
    first = create_job(request=request(lead_id=lead["id"]))

    def enqueue() -> int:
        return SqliteContactStore(path).enqueue_research_job(first).id

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: enqueue(), range(2)))
    assert ids[0] == ids[1]
    with db.get_conn(path) as conn:
        assert conn.execute("SELECT COUNT(*) AS count FROM research_jobs").fetchone()["count"] == 1


def test_get_and_list_are_typed_bounded_filtered_and_deterministic(tmp_path):
    path, lead, *_ = setup_db(tmp_path)
    store = SqliteContactStore(path)
    low = store.enqueue_research_job(create_job(request=request(lead_id=lead["id"], target_roles=["workflow_user"], job_id="low"), priority=10))
    high = store.enqueue_research_job(create_job(request=request(lead_id=lead["id"], target_roles=["technical_influencer"], job_id="high"), priority=90))
    assert isinstance(store.get_research_job(high.id).status, ResearchJobStatus)
    assert [job.id for job in store.list_research_jobs_for_lead(lead["id"])] == [high.id, low.id]
    assert store.list_research_jobs(status="queued", limit=1)[0].id == high.id
    with pytest.raises(ResearchJobError) as invalid:
        store.list_research_jobs(status="not-a-status")
    assert invalid.value.code == "invalid_status_filter"
    with pytest.raises(ResearchJobError):
        store.list_research_jobs(limit=0)
    with pytest.raises(ValidationError):
        store.get_research_job(high.id).priority = 1
    with pytest.raises(ResearchJobError) as missing:
        store.get_research_job(99999)
    assert missing.value.code == "job_not_found"


def test_secret_safety_and_snapshot_keys_are_preserved_without_arbitrary_extensions(tmp_path):
    path, lead, *_ = setup_db(tmp_path)
    with pytest.raises(ValidationError) as invalid:
        request(lead_id=lead["id"], provider_config_ref={"api_key": "secret-value"})
    assert "secret-value" not in str(invalid.value)
    job = create_job(request=request(lead_id=lead["id"]))
    stored = SqliteContactStore(path).enqueue_research_job(job)
    assert "api_key" not in stored.request_snapshot_json
    assert "password" not in stored.request_snapshot_json
    assert set(json.loads(stored.request_snapshot_json)) == set(job.request.model_dump(mode="json"))


def test_enqueue_isolation_and_rollback_preserve_all_existing_data(tmp_path):
    path, lead, _, person, person_candidate, contact_candidate = setup_db(tmp_path)
    with db.get_conn(path) as conn:
        before = {
            table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
            for table in ("leads", "people", "tasks", "interactions", "person_candidates", "contact_method_candidates")
        }
        conn.execute("CREATE TRIGGER fail_research_job_insert BEFORE INSERT ON research_jobs BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(ResearchJobError) as failed:
        SqliteContactStore(path).enqueue_research_job(create_job(request=request(lead_id=lead["id"])))
    assert failed.value.code == "enqueue_persistence_failure"
    with db.get_conn(path) as conn:
        conn.execute("DROP TRIGGER fail_research_job_insert")
        after = {
            table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
            for table in ("leads", "people", "tasks", "interactions", "person_candidates", "contact_method_candidates")
        }
        assert conn.execute("SELECT COUNT(*) AS count FROM research_jobs").fetchone()["count"] == 0
    assert after == before
    assert person["id"] and person_candidate["id"] and contact_candidate["id"]
    assert SqliteContactStore(path).list_research_jobs() == []


def test_unrelated_integrity_failure_does_not_hide_existing_active_job(tmp_path):
    path, lead, *_ = setup_db(tmp_path)
    store = SqliteContactStore(path)
    job = create_job(request=request(lead_id=lead["id"]))
    original = store.enqueue_research_job(job)
    with db.get_conn(path) as conn:
        conn.execute(
            """CREATE TRIGGER force_unrelated_research_job_integrity_failure
               BEFORE INSERT ON research_jobs
               BEGIN SELECT RAISE(ABORT, 'forced_unrelated_integrity_failure'); END"""
        )

    with pytest.raises(ResearchJobError) as failed:
        store.enqueue_research_job(job)
    assert failed.value.code == "enqueue_persistence_failure"
    assert "forced_unrelated_integrity_failure" not in str(failed.value)
    assert store.get_research_job(original.id) == original
    assert len(store.list_research_jobs()) == 1

    with db.get_conn(path) as conn:
        conn.execute("DROP TRIGGER force_unrelated_research_job_integrity_failure")
    assert store.get_research_job(original.id).id == original.id
