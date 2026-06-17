"""Regression tests for lead intake polish."""

from unittest.mock import patch

import db
from extractor import deterministic_parse, parse_and_save, parse_source_filter_tier


SHOPIFY_SAMPLE = """
Shero Commerce
Shopify Plus Partner
https://sherocommerce.com
Services: Store setup, Migration, Shopify Plus, CRO
"""

PLUS_DIRECTORY_URL = (
    "https://partners.shopify.com/directory/search?"
    "partnerTiers=tier_plus&services=development"
)


def _save(db_path, text=SHOPIFY_SAMPLE, source_url=None):
    with patch("extractor.extract_structured", return_value=(None, "")):
        return parse_and_save(
            "shopify_directory",
            text,
            source_url=source_url,
            db_path=db_path,
        )


def test_duplicate_paste_one_lead_two_raw_sources(tmp_path):
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    r1 = _save(db_path)
    r2 = _save(db_path)
    assert r1["lead_id"] == r2["lead_id"]
    with db.get_conn(db_path) as conn:
        lead_count = conn.execute("SELECT COUNT(*) AS c FROM leads").fetchone()["c"]
        source_count = conn.execute("SELECT COUNT(*) AS c FROM raw_sources").fetchone()["c"]
    assert lead_count == 1
    assert source_count == 2


def test_source_filter_tier_from_url(tmp_path):
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    assert parse_source_filter_tier(PLUS_DIRECTORY_URL) == "tier_plus"
    result = _save(db_path, source_url=PLUS_DIRECTORY_URL)
    with db.get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT source_filter_tier FROM raw_sources WHERE id = ?",
            (result["raw_source_id"],),
        ).fetchone()
    lead = db.get_lead(result["lead_id"], db_path=db_path)
    assert row["source_filter_tier"] == "tier_plus"
    assert lead["source_filter_tier"] == "tier_plus"
    assert lead["partner_tier"] is not None
    assert "Plus" in lead["partner_tier"]
    assert lead["partner_tier"] != "tier_plus"


def test_company_name_preserves_first_character(tmp_path):
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    samples = [
        "Shero Commerce\nPlus Partner",
        "2IWD Agency\nShopify Partner",
        "\ufeff3Blackbelt\nPlus Partner",
    ]
    for text in samples:
        parsed = deterministic_parse("shopify_directory", text)
        expected = db.sanitize_company_name(text.splitlines()[0].strip())
        assert parsed["company_name"] == expected
        assert parsed["company_name"][0] == expected[0]

    result = _save(db_path, text="2IWD Agency\nShopify Plus Partner\nhttps://2iwd.com")
    lead = db.get_lead(result["lead_id"], db_path=db_path)
    assert lead["company_name"] == "2IWD Agency"
    assert lead["company_name"][0] == "2"

    # Simulate bad update that would drop first character
    lead_id = result["lead_id"]
    db.upsert_lead({"company_name": "IWD Agency"}, lead_id=lead_id, db_path=db_path)
    lead = db.get_lead(lead_id, db_path=db_path)
    assert lead["company_name"] == "2IWD Agency"


def test_canonical_source_ordering(tmp_path):
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    _save(db_path, text="Shero Commerce\nPass one")
    _save(db_path, text="Shero Commerce\nPass two")
    lead = db.get_lead(1, db_path=db_path)
    assert lead["canonical_source"]["raw_text"].strip().endswith("Pass two")
    assert len(lead["source_history"]) == 1
    assert "Pass one" in lead["source_history"][0]["raw_text"]
