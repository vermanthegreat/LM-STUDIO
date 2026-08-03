"""Regression coverage for controlled person enrichment."""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import db
import pytest
from extractor import parse_and_save
from scoring import classify_person_title


def test_precise_role_classification() -> None:
    expected = {
        "Founder": ("economic_buyer", True, True),
        "CEO": ("economic_buyer", True, True),
        "Owner": ("economic_buyer", True, True),
        "Managing Partner": ("economic_buyer", True, True),
        "Head of Ecommerce": ("operational_owner", True, True),
        "Director of Ecommerce": ("operational_owner", True, True),
        "VP Ecommerce": ("operational_owner", True, True),
        "Head of Shopify": ("operational_owner", True, True),
        "Partnerships Manager": ("workflow_user", False, True),
        "Client Success Manager": ("workflow_user", False, True),
        "Operations Director": ("operational_owner", True, True),
        "Operations Specialist": ("operational_owner", False, True),
        "Project Manager": ("workflow_user", False, True),
        "Solutions Architect": ("technical_influencer", False, True),
    }
    for title, result in expected.items():
        classified = classify_person_title(title)
        assert (
            classified["role_type"],
            classified["is_decision_maker"],
            classified["is_relevant_contact"],
        ) == result

    for title in (
        "Partner",
        "Operations Analyst",
        "Product Owner",
        "Assistant to the CEO",
        "CEO Assistant",
    ):
        classified = classify_person_title(title)
        assert classified["role_type"] == "other"
        assert classified["is_decision_maker"] is False


def _lead(db_path):
    db.init_db(db_path)
    return db.upsert_lead({"company_name": "Example Co"}, db_path=db_path)[0]


def test_role_type_is_authoritative_and_legacy_booleans_fail_closed(tmp_path) -> None:
    db_path = tmp_path / "role-inputs.db"
    lead = _lead(db_path)
    role_only = db.add_person(
        lead["id"],
        {"name": "Role Only", "role_type": "economic_buyer"},
        db_path=db_path,
    )
    booleans_only = db.add_person(
        lead["id"],
        {
            "name": "Booleans Only",
            "is_decision_maker": True,
            "is_relevant_contact": True,
        },
        db_path=db_path,
    )
    conflict = db.add_person(
        lead["id"],
        {
            "name": "Conflict",
            "title": "Project Manager",
            "role_type": "economic_buyer",
            "is_decision_maker": False,
        },
        db_path=db_path,
    )

    assert (role_only["role_type"], role_only["is_decision_maker"]) == (
        "economic_buyer", 1
    )
    assert (booleans_only["role_type"], booleans_only["is_decision_maker"]) == (
        "other", 0
    )
    assert (conflict["role_type"], conflict["is_decision_maker"]) == (
        "economic_buyer", 1
    )
    with pytest.raises(ValueError, match="Unsupported role_type"):
        db.add_person(
            lead["id"], {"name": "Invalid", "role_type": "partner"}, db_path=db_path
        )


def test_person_dedup_priority_and_name_fallback(tmp_path) -> None:
    db_path = tmp_path / "dedup.db"
    lead = _lead(db_path)

    linkedin_first = db.add_person(
        lead["id"],
        {"name": "Jane One", "title": "Founder", "linkedin_url": "https://linkedin.com/in/jane"},
        db_path=db_path,
    )
    linkedin_second = db.add_person(
        lead["id"],
        {"name": "Jane Updated", "title": "Founder", "linkedin_url": "https://linkedin.com/in/jane/"},
        db_path=db_path,
    )
    email_first = db.add_person(
        lead["id"],
        {"name": "Sam One", "title": "Project Manager", "email": "Sam@Example.com"},
        db_path=db_path,
    )
    email_second = db.add_person(
        lead["id"],
        {"name": "Sam Updated", "title": "Project Manager", "email": "sam@example.com"},
        db_path=db_path,
    )
    name_first = db.add_person(
        lead["id"], {"name": "Alex Smith", "title": "Solutions Architect"}, db_path=db_path
    )
    name_second = db.add_person(
        lead["id"], {"name": " alex  smith ", "title": "Technical Director"}, db_path=db_path
    )

    assert linkedin_first["id"] == linkedin_second["id"]
    assert email_first["id"] == email_second["id"]
    assert name_first["id"] == name_second["id"]


def test_name_fallback_does_not_merge_conflicting_functions_or_empty_names(tmp_path) -> None:
    db_path = tmp_path / "conflicts.db"
    lead = _lead(db_path)
    first = db.add_person(
        lead["id"], {"name": "Taylor Lee", "title": "Operations Director"}, db_path=db_path
    )
    second = db.add_person(
        lead["id"], {"name": "Taylor Lee", "title": "Solutions Architect"}, db_path=db_path
    )
    empty_one = db.add_person(lead["id"], {"name": "", "title": "Founder"}, db_path=db_path)
    empty_two = db.add_person(lead["id"], {"name": "", "title": "Founder"}, db_path=db_path)

    assert first["id"] != second["id"]
    assert empty_one["id"] != empty_two["id"]


def test_name_fallback_is_lead_scoped_and_preserves_stronger_role(tmp_path) -> None:
    db_path = tmp_path / "lead-scope.db"
    first_lead = _lead(db_path)
    second_lead = db.upsert_lead({"company_name": "Other Co"}, db_path=db_path)[0]
    original = db.add_person(
        first_lead["id"],
        {
            "name": "Alex Smith",
            "title": "Founder",
            "email": "alex@example.com",
        },
        db_path=db_path,
    )
    repeated = db.add_person(
        first_lead["id"],
        {"name": " alex  smith ", "title": None},
        db_path=db_path,
    )
    other_company = db.add_person(
        second_lead["id"], {"name": "Alex Smith", "title": "Founder"}, db_path=db_path
    )

    assert repeated["id"] == original["id"]
    assert repeated["role_type"] == "economic_buyer"
    assert repeated["is_decision_maker"] == 1
    assert repeated["email"] == "alex@example.com"
    assert other_company["id"] != original["id"]


def test_safe_sqlite_migration_adds_and_backfills_enrichment_fields(tmp_path) -> None:
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        legacy_schema = db.SCHEMA
        for line in (
            "    enrichment_status TEXT DEFAULT 'pending',\n",
            "    role_type TEXT,\n",
            "    email_status TEXT DEFAULT 'unknown',\n",
            "    email_confidence REAL DEFAULT 0.0,\n",
            "    last_verified_at TEXT,\n",
            "    updated_at TEXT\n",
        ):
            legacy_schema = legacy_schema.replace(line, "")
        legacy_schema = legacy_schema.replace("    created_at TEXT NOT NULL,\n);", "    created_at TEXT NOT NULL\n);")
        conn.executescript(legacy_schema)
        conn.execute(
            "INSERT INTO leads (id, company_name, created_at, updated_at) VALUES (1, ?, ?, ?)",
            ("Legacy Co", "2026-01-01", "2026-01-01"),
        )
        conn.execute(
            """INSERT INTO people
               (id, lead_id, name, title, is_decision_maker, is_relevant_contact,
                confidence, created_at)
               VALUES (1, 1, 'Pat', 'Partnerships Manager', 1, 1, 0.5, '2026-01-01')"""
        )

    db.init_db(db_path)
    with db.get_conn(db_path) as conn:
        lead = conn.execute("SELECT enrichment_status FROM leads WHERE id = 1").fetchone()
        person = conn.execute(
            "SELECT role_type, is_decision_maker, email_status, email_confidence, updated_at "
            "FROM people WHERE id = 1"
        ).fetchone()

    assert lead["enrichment_status"] == "pending"
    assert person["role_type"] == "workflow_user"
    assert person["is_decision_maker"] == 0
    assert person["email_status"] == "unknown"
    assert person["email_confidence"] == 0.0
    assert person["updated_at"] == "2026-01-01"


def test_research_filter_and_sorting(tmp_path) -> None:
    db_path = tmp_path / "research.db"
    db.init_db(db_path)
    high, _ = db.upsert_lead(
        {"company_name": "High No DM", "fit_score": 90, "enrichment_status": "pending"},
        db_path=db_path,
    )
    ready, _ = db.upsert_lead(
        {"company_name": "Ready", "fit_score": 100, "enrichment_status": "ready"},
        db_path=db_path,
    )
    covered, _ = db.upsert_lead(
        {"company_name": "Covered", "fit_score": 80, "enrichment_status": "pending"},
        db_path=db_path,
    )
    db.add_person(covered["id"], {"name": "Owner", "title": "Founder"}, db_path=db_path)
    db.add_person(covered["id"], {"name": "PM", "title": "Project Manager"}, db_path=db_path)
    review, _ = db.upsert_lead(
        {"company_name": "Review", "fit_score": 70, "enrichment_status": "needs_review"},
        db_path=db_path,
    )

    rows = db.list_leads(db_path=db_path, research_only=True)

    assert [row["id"] for row in rows] == [high["id"], review["id"]]
    assert ready["id"] not in {row["id"] for row in rows}


def test_extractor_never_accepts_unproven_verified_email(tmp_path) -> None:
    db_path = tmp_path / "email-status.db"
    db.init_db(db_path)
    payload = {
        "company_name": "Published Co",
        "website": "https://published.example",
        "people": [{
            "name": "Casey Jones",
            "title": "Founder",
            "email": "casey@published.example",
            "email_status": "verified",
            "email_confidence": 0.9,
        }],
        "confidence": 0.8,
    }
    with patch("extractor.extract_structured", return_value=(payload, '{"ok": true}')):
        result = parse_and_save("website", "Published Co", db_path=db_path)

    person = db.get_lead(result["lead_id"], db_path=db_path)["people"][0]
    assert person["email_status"] == "published"
    assert person["email_confidence"] == 0.9


def test_email_metadata_is_bounded_and_tied_to_the_incoming_email(tmp_path) -> None:
    db_path = tmp_path / "email-metadata.db"
    lead = _lead(db_path)
    original = db.add_person(
        lead["id"],
        {
            "name": "Casey Jones",
            "title": "Founder",
            "email": "casey@example.com",
            "email_status": "unknown",
            "email_confidence": 0.4,
        },
        db_path=db_path,
    )
    repeated = db.add_person(
        lead["id"],
        {
            "name": "Casey Jones",
            "title": None,
            "email_status": "verified",
            "email_confidence": 1.0,
            "last_verified_at": "2026-01-02T00:00:00+00:00",
        },
        db_path=db_path,
    )

    assert repeated["id"] == original["id"]
    assert repeated["email_status"] == "unknown"
    assert repeated["email_confidence"] == 0.4
    assert repeated["last_verified_at"] is None

    with pytest.raises(ValueError, match="email_confidence"):
        db.add_person(
            lead["id"],
            {
                "name": "Invalid Confidence",
                "email": "invalid@example.com",
                "email_confidence": 1.1,
            },
            db_path=db_path,
        )

    verified = db.add_person(
        lead["id"],
        {
            "name": "Verified Person",
            "email": "verified@example.com",
            "email_status": "verified",
            "email_confidence": 1.0,
            "last_verified_at": "2026-01-02T00:00:00+00:00",
        },
        db_path=db_path,
    )
    downgraded = db.add_person(
        lead["id"],
        {
            "name": "Verified Person",
            "email": "verified@example.com",
            "email_status": "verified",
            "email_confidence": 1.0,
        },
        db_path=db_path,
    )
    assert verified["email_status"] == "verified"
    assert downgraded["email_status"] == "verified"
    assert downgraded["last_verified_at"] == "2026-01-02T00:00:00+00:00"


def test_note_email_is_not_marked_published(tmp_path) -> None:
    db_path = tmp_path / "note-email.db"
    db.init_db(db_path)
    payload = {
        "company_name": "Notes Co",
        "people": [{"name": "Pat Doe", "title": "Project Manager", "email": "pat@example.com"}],
        "confidence": 0.8,
    }
    with patch("extractor.extract_structured", return_value=(payload, '{"ok": true}')):
        result = parse_and_save("note", "Internal operator note", db_path=db_path)

    person = db.get_lead(result["lead_id"], db_path=db_path)["people"][0]
    assert person["email_status"] == "unknown"
    assert person["last_verified_at"] is None
