"""Tests for deterministic extraction fallback."""

import tempfile
from pathlib import Path

import db
from extractor import deterministic_parse, parse_and_save


SHOPIFY_SAMPLE = """
Shero Commerce
Shopify Plus Partner
https://sherocommerce.com
Services: Store setup, Migration, Shopify Plus, CRO
New York, USA
"""

LINKEDIN_PERSON = """
Jane Doe
CEO at Acme Agency
https://www.linkedin.com/in/janedoe
Experience
"""


def test_shopify_directory_fallback():
    parsed = deterministic_parse("shopify_directory", SHOPIFY_SAMPLE)
    assert parsed["company_name"] == "Shero Commerce"
    assert parsed["partner_tier"] is not None
    assert "Plus" in parsed["partner_tier"]
    assert parsed["website"] is not None
    assert "sherocommerce.com" in parsed["website"]


def test_linkedin_person_fallback():
    parsed = deterministic_parse("linkedin_person", LINKEDIN_PERSON)
    assert parsed["people"]
    assert parsed["people"][0]["name"] == "Jane Doe"
    assert "CEO" in (parsed["people"][0]["title"] or "")


def test_parse_and_save_never_discards(tmp_path):
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    result = parse_and_save(
        "shopify_directory",
        SHOPIFY_SAMPLE,
        db_path=db_path,
    )
    assert result["raw_source_id"]
    with db.get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT raw_text, extraction_status FROM raw_sources WHERE id = ?",
            (result["raw_source_id"],),
        ).fetchone()
    assert row["raw_text"] == SHOPIFY_SAMPLE
    assert row["extraction_status"] in ("ok", "fallback", "needs_review")
