"""Isolated SQLite tests for noncanonical enrichment candidates."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier

import db
import pytest
from candidate_models import CandidateError


def setup_case(tmp_path):
    path = tmp_path / "candidates.db"
    db.init_db(path)
    lead, _ = db.upsert_lead({"company_name": "Example", "company_email": "company@example.com", "company_phone": "1", "website": "https://example.com"}, db_path=path)
    other, _ = db.upsert_lead({"company_name": "Other"}, db_path=path)
    source = db.create_raw_source("note", "source", lead_id=lead["id"], db_path=path)
    other_source = db.create_raw_source("note", "other", lead_id=other["id"], db_path=path)
    return path, lead, other, source, other_source


def person(path, lead, source, **overrides):
    data = {"name": "Jane Doe", "title": "Founder", "role_type": "economic_buyer", "is_decision_maker": True, "source_type": "note", "discovery_method": "manual_paste", "confidence": 0.8}
    data.update(overrides)
    return db.create_or_reuse_person_candidate(lead["id"], source["id"], db_path=path, **data)


def test_schema_repeatable_and_preserves_existing_records(tmp_path):
    path, lead, _, source, _ = setup_case(tmp_path)
    p = person(path, lead, source)
    db.init_db(path)
    assert db.get_person_candidate(p["id"], db_path=path)["id"] == p["id"]
    with db.get_conn(path) as conn:
        names = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"person_candidates", "contact_method_candidates", "leads", "people"} <= names


def test_person_create_list_reuse_and_scope(tmp_path):
    path, lead, other, source, other_source = setup_case(tmp_path)
    same_lead_source = db.create_raw_source("note", "second source", lead_id=lead["id"], db_path=path)
    first = person(path, lead, source)
    assert person(path, lead, source)["id"] == first["id"]
    assert person(path, lead, source, name="Different", profile_url="https://linkedin.com/in/jane")["id"] != first["id"]
    assert person(path, lead, same_lead_source, name="Jane Doe")["id"] != first["id"]
    assert person(path, other, other_source)["id"] != first["id"]
    assert {row["id"] for row in db.list_person_candidates_for_lead(lead["id"], db_path=path)} == {first["id"], first["id"] + 1, first["id"] + 2}


def test_contact_create_retrieve_list_and_reuse(tmp_path):
    path, lead, _, source, _ = setup_case(tmp_path)
    candidate = person(path, lead, source)
    first = db.create_or_reuse_contact_candidate(lead["id"], source["id"], person_candidate_id=candidate["id"], kind="email", value="Jane@Example.com", discovery_method="manual_paste", db_path=path)
    second = db.create_or_reuse_contact_candidate(lead["id"], source["id"], person_candidate_id=candidate["id"], kind="email", value="jane@example.com", discovery_method="manual_paste", db_path=path)
    assert second["id"] == first["id"]
    assert db.get_contact_candidate(first["id"], db_path=path)["normalized_value"] == "jane@example.com"
    assert len(db.list_contact_candidates_for_lead(lead["id"], db_path=path)) == 1


def test_ownership_confidence_and_name_validation(tmp_path):
    path, lead, other, source, other_source = setup_case(tmp_path)
    with pytest.raises(CandidateError) as missing_lead:
        person(path, {"id": 999}, source)
    assert missing_lead.value.code == "lead_not_found"
    with pytest.raises(CandidateError) as wrong_source:
        person(path, lead, other_source)
    assert wrong_source.value.code == "raw_source_ownership_mismatch"
    with pytest.raises(CandidateError) as low_confidence:
        person(path, lead, source, confidence=-0.1)
    assert low_confidence.value.code == "invalid_confidence"
    with pytest.raises(CandidateError) as high_confidence:
        person(path, lead, source, confidence=1.1)
    assert high_confidence.value.code == "invalid_confidence"
    with pytest.raises(CandidateError) as blank_name:
        person(path, lead, source, name=" ")
    assert blank_name.value.code == "invalid_candidate_name"


def test_contact_verification_and_owner_rules(tmp_path):
    path, lead, other, source, other_source = setup_case(tmp_path)
    candidate = person(path, lead, source)
    with pytest.raises(CandidateError) as no_owner:
        db.create_or_reuse_contact_candidate(lead["id"], source["id"], kind="email", value="a@example.com", discovery_method="manual_paste", db_path=path)
    assert no_owner.value.code == "invalid_owner_binding"
    with pytest.raises(CandidateError) as two_owners:
        db.create_or_reuse_contact_candidate(lead["id"], source["id"], person_candidate_id=candidate["id"], person_id=1, kind="email", value="a@example.com", discovery_method="manual_paste", db_path=path)
    assert two_owners.value.code == "invalid_owner_binding"
    for status in ("source_confirmed", "verified"):
        with pytest.raises(CandidateError) as invalid_evidence:
            db.create_or_reuse_contact_candidate(lead["id"], source["id"], person_candidate_id=candidate["id"], kind="email", value="a@example.com", evidence_basis="inferred", verification_status=status, verified_at="2026-01-01T00:00:00+00:00" if status == "verified" else None, discovery_method="manual_paste", db_path=path)
        assert invalid_evidence.value.code == "invalid_verification_state"
    with pytest.raises(CandidateError) as missing_verified_at:
        db.create_or_reuse_contact_candidate(lead["id"], source["id"], person_candidate_id=candidate["id"], kind="email", value="a@example.com", verification_status="verified", discovery_method="manual_paste", db_path=path)
    assert missing_verified_at.value.code == "invalid_verification_state"
    with pytest.raises(CandidateError) as unexpected_verified_at:
        db.create_or_reuse_contact_candidate(lead["id"], source["id"], person_candidate_id=candidate["id"], kind="email", value="a@example.com", verified_at="2026-01-01T00:00:00+00:00", discovery_method="manual_paste", db_path=path)
    assert unexpected_verified_at.value.code == "invalid_verification_state"
    with pytest.raises(CandidateError) as wrong_owner:
        db.create_or_reuse_contact_candidate(lead["id"], source["id"], person_candidate_id=person(path, other, other_source)["id"], kind="email", value="a@example.com", discovery_method="manual_paste", db_path=path)
    assert wrong_owner.value.code == "person_candidate_ownership_mismatch"


def test_versioned_status_updates_and_binding_invariants(tmp_path):
    path, lead, _, source, _ = setup_case(tmp_path)
    candidate = person(path, lead, source)
    updated = db.update_person_candidate_status(candidate["id"], 1, "approved", db_path=path)
    assert updated["version"] == 2
    with pytest.raises(CandidateError) as stale:
        db.update_person_candidate_status(candidate["id"], 1, "rejected", db_path=path)
    assert stale.value.code == "stale_candidate_version"
    with pytest.raises(CandidateError) as non_applied_binding:
        db.update_person_candidate_status(candidate["id"], 2, "approved", applied_person_id=1, db_path=path)
    assert non_applied_binding.value.code == "invalid_applied_binding"
    with pytest.raises(CandidateError) as missing_applied_binding:
        db.update_person_candidate_status(candidate["id"], 2, "applied", db_path=path)
    assert missing_applied_binding.value.code == "invalid_applied_binding"


class _InterleavingConnection:
    def __init__(self, connection, path, table, candidate_id):
        self.connection = connection
        self.path = path
        self.table = table
        self.candidate_id = candidate_id
        self.interleaved = False

    def execute(self, sql, parameters=()):
        if not self.interleaved and sql.lstrip().startswith(f"UPDATE {self.table} SET"):
            with db.get_conn(self.path) as concurrent:
                concurrent.execute(
                    f"UPDATE {self.table} SET status = 'conflict', version = version + 1 WHERE id = ?",
                    (self.candidate_id,),
                )
            self.interleaved = True
        return self.connection.execute(sql, parameters)


@contextmanager
def _interleaved_candidate_connection(path, table, candidate_id):
    with db.get_conn(path) as connection:
        yield _InterleavingConnection(connection, path, table, candidate_id)


def test_person_status_update_rejects_zero_row_race(tmp_path, monkeypatch):
    path, lead, _, source, _ = setup_case(tmp_path)
    candidate = person(path, lead, source)
    canonical = db.add_person(lead["id"], {"name": "Jane Doe"}, db_path=path)
    monkeypatch.setattr(
        db,
        "_candidate_connection",
        lambda db_path, conn: _interleaved_candidate_connection(path, "person_candidates", candidate["id"]),
    )

    with pytest.raises(CandidateError) as error:
        db.update_person_candidate_status(
            candidate["id"], 1, "applied", applied_person_id=canonical["id"], db_path=path
        )

    assert error.value.code == "stale_candidate_version"
    stored = db.get_person_candidate(candidate["id"], db_path=path)
    assert stored["status"] == "conflict"
    assert stored["version"] == 2
    assert stored["applied_person_id"] is None


def test_contact_status_update_rejects_zero_row_race(tmp_path, monkeypatch):
    path, lead, _, source, _ = setup_case(tmp_path)
    candidate = person(path, lead, source)
    contact = db.create_or_reuse_contact_candidate(
        lead["id"],
        source["id"],
        person_candidate_id=candidate["id"],
        kind="email",
        value="jane@example.com",
        discovery_method="manual_paste",
        db_path=path,
    )
    monkeypatch.setattr(
        db,
        "_candidate_connection",
        lambda db_path, conn: _interleaved_candidate_connection(path, "contact_method_candidates", contact["id"]),
    )

    with pytest.raises(CandidateError) as error:
        db.update_contact_candidate_status(
            contact["id"], 1, "applied", applied_contact_method_id=99, db_path=path
        )

    assert error.value.code == "stale_candidate_version"
    stored = db.get_contact_candidate(contact["id"], db_path=path)
    assert stored["status"] == "conflict"
    assert stored["version"] == 2
    assert stored["applied_contact_method_id"] is None


def test_person_create_or_reuse_serializes_parallel_identical_calls(tmp_path):
    path, lead, _, source, _ = setup_case(tmp_path)
    barrier = Barrier(2)

    def create():
        barrier.wait()
        return person(path, lead, source)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: create(), range(2)))

    assert {result["id"] for result in results} == {1}
    assert len(db.list_person_candidates_for_lead(lead["id"], db_path=path)) == 1
    stored = db.get_person_candidate(1, db_path=path)
    assert stored["version"] == 1
    assert stored["status"] == "needs_review"
    assert db.get_lead(lead["id"], db_path=path)["people"] == []


def test_contact_create_or_reuse_serializes_parallel_identical_calls(tmp_path):
    path, lead, _, source, _ = setup_case(tmp_path)
    candidate = person(path, lead, source)
    barrier = Barrier(2)

    def create():
        barrier.wait()
        return db.create_or_reuse_contact_candidate(
            lead["id"],
            source["id"],
            person_candidate_id=candidate["id"],
            kind="email",
            value="jane@example.com",
            discovery_method="manual_paste",
            db_path=path,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: create(), range(2)))

    assert {result["id"] for result in results} == {1}
    assert len(db.list_contact_candidates_for_lead(lead["id"], db_path=path)) == 1
    stored = db.get_contact_candidate(1, db_path=path)
    assert stored["version"] == 1
    assert stored["status"] == "needs_review"
    assert stored["person_candidate_id"] == candidate["id"]
    lead_detail = db.get_lead(lead["id"], db_path=path)
    assert lead_detail["people"] == []
    assert lead_detail["tasks"] == []
    assert lead_detail["interactions"] == []


def test_candidate_creation_does_not_mutate_canonical_records(tmp_path):
    path, lead, _, source, _ = setup_case(tmp_path)
    before = db.get_lead(lead["id"], db_path=path)
    person(path, lead, source)
    contact = db.create_or_reuse_contact_candidate(lead["id"], source["id"], person_candidate_id=1, kind="email", value="a@example.com", discovery_method="manual_paste", db_path=path)
    after = db.get_lead(lead["id"], db_path=path)
    assert contact["id"] == 1
    assert after["people"] == before["people"] == []
    assert {key: after[key] for key in ("company_email", "company_phone", "website")} == {key: before[key] for key in ("company_email", "company_phone", "website")}
    assert after["tasks"] == before["tasks"] == []
    assert after["interactions"] == before["interactions"] == []


def test_candidate_creation_rolls_back_on_persistence_failure(tmp_path, monkeypatch):
    path, lead, _, source, _ = setup_case(tmp_path)
    with db.get_conn(path) as conn:
        conn.execute("CREATE TRIGGER fail_candidate_insert AFTER INSERT ON person_candidates BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        person(path, lead, source)
    # The failed transaction must not leave a candidate behind.
    with db.get_conn(path) as conn:
        conn.execute("DROP TRIGGER fail_candidate_insert")
    assert db.list_person_candidates_for_lead(lead["id"], db_path=path) == []
