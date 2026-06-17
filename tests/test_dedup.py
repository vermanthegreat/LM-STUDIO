"""Tests for lead deduplication."""

import tempfile

import db


def test_dedup_by_normalized_name(tmp_path):
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    lead1, _ = db.upsert_lead({"company_name": "Acme Agency"}, db_path=db_path)
    lead2, is_new = db.upsert_lead({"company_name": "ACME  Agency"}, db_path=db_path)
    assert not is_new
    assert lead1["id"] == lead2["id"]
    assert lead2["normalized_name"] == "acme agency"


def test_dedup_by_domain(tmp_path):
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    db.upsert_lead(
        {"company_name": "Foo Corp", "website": "https://www.foocorp.com"},
        db_path=db_path,
    )
    matches = db.find_matching_leads(website="https://foocorp.com/about", db_path=db_path)
    assert len(matches) == 1
    assert matches[0]["domain"] == "foocorp.com"


def test_possible_duplicate_flag(tmp_path):
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    db.upsert_lead({"company_name": "Alpha LLC", "website": "https://alpha.com"}, db_path=db_path)
    lead, _ = db.upsert_lead(
        {"company_name": "Beta Inc", "website": "https://alpha.com"},
        db_path=db_path,
    )
    assert lead["domain"] == "alpha.com"
