"""Regression tests for Shopify directory fallback (x-mozo fixture)."""

from pathlib import Path
from unittest.mock import patch

import db
from extractor import deterministic_parse, parse_and_save

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "x_mozo_shopify.txt"
X_MOZO_TEXT = FIXTURE_PATH.read_text(encoding="utf-8")

EXPECTED_SERVICES = [
    "Store migration",
    "Theme customization",
    "Store build or redesign",
    "Systems integration",
    "Store settings configuration",
    "Product and collection setup",
]

EXPECTED_INDUSTRIES = [
    "Clothing and fashion",
    "Health and beauty",
    "Home and garden",
    "Jewelry and accessories",
]

EXPECTED_FEATURED_WORK_TITLES = [
    "NOOSH",
    "Shlomit Ofir",
    "Ronen Chen",
    "The Moon Stoned",
    "RC Cola International",
    "B Unique",
]


def test_x_mozo_fallback_extracts_contact_and_profile_fields():
    parsed = deterministic_parse("shopify_directory", X_MOZO_TEXT)

    assert parsed["company_name"] == "x-mozo"
    assert parsed["website"] == "x-mozo.com"
    assert parsed["company_email"] == "[Hello@x-mozo.com](mailto:Hello@x-mozo.com)"
    assert parsed["company_email"] != "[shecsar@gmail.com](mailto:shecsar@gmail.com)"
    assert parsed["company_phone"] == "+972-524788739"
    assert parsed["partner_since"] == "February 2020"
    assert parsed["primary_location"] == "yehud, Israel"
    assert "Israel" in parsed["supported_locations"]
    assert "United States" in parsed["supported_locations"]
    assert "English" in parsed["languages"]
    assert "Hebrew" in parsed["languages"]
    assert parsed["description"].startswith("X-Mozo is a boutique e-commerce company")
    assert "Service partner" in parsed["partner_tier"]
    assert parsed["plus_partner_signal"] is True
    assert parsed["rating"] == 5.0
    assert parsed["review_count"] == 4
    assert parsed["raw_text"] == X_MOZO_TEXT

    for svc in EXPECTED_SERVICES:
        assert svc in parsed["services"]
    for ind in EXPECTED_INDUSTRIES:
        assert ind in parsed["industries"]
    featured_titles = [item.get("title", "") for item in (parsed.get("featured_work") or [])]
    for title in EXPECTED_FEATURED_WORK_TITLES:
        assert any(title in ft for ft in featured_titles)

    assert parsed["description"] != "Service partner 5.0 (4)"
    assert "Service partner 5.0 (4)" not in (parsed["description"] or "")


def test_x_mozo_description_not_tier_rating_line():
    parsed = deterministic_parse("shopify_directory", X_MOZO_TEXT)
    assert not parsed["description"].startswith("Service partner 5.0")


def test_x_mozo_parse_and_save_links_raw_source(tmp_path):
    db_path = tmp_path / "test.db"
    db.init_db(db_path)

    with patch("extractor.extract_structured", return_value=(None, "")):
        result = parse_and_save(
            "shopify_directory",
            X_MOZO_TEXT,
            source_url="https://partners.shopify.com/directory/search?partnerTiers=tier_plus",
            db_path=db_path,
        )

    assert result["lead_id"]
    assert result["raw_source_id"]

    lead = db.get_lead(result["lead_id"], db_path=db_path)
    assert lead is not None
    assert len(lead["raw_sources"]) == 1
    assert lead["canonical_source"] is not None
    assert lead["canonical_source"]["id"] == result["raw_source_id"]
    assert lead["canonical_source"]["lead_id"] == result["lead_id"]
    assert lead["canonical_source"]["raw_text"] == X_MOZO_TEXT
    assert len(lead["source_history"]) == 0

    assert lead["website"] == "x-mozo.com"
    assert lead["company_email"] == "[Hello@x-mozo.com](mailto:Hello@x-mozo.com)"
    assert lead["company_email"] != "[shecsar@gmail.com](mailto:shecsar@gmail.com)"
    assert lead["company_phone"] == "+972-524788739"
    assert lead["partner_since"] == "February 2020"
    assert lead["primary_location"] == "yehud, Israel"
    assert "Israel" in (lead["supported_locations"] or [])
    assert "United States" in (lead["supported_locations"] or [])
    assert "English" in (lead["languages"] or [])
    assert "Hebrew" in (lead["languages"] or [])
    assert (lead["description"] or "").startswith("X-Mozo is a boutique e-commerce company")
    assert lead["rating"] == 5.0
    assert lead["review_count"] == 4
    assert lead["plus_partner_signal"] is True
    assert lead["source_filter_tier"] == "tier_plus"
    assert lead["partner_tier"] != lead["source_filter_tier"]

    with db.get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT lead_id FROM raw_sources WHERE id = ?",
            (result["raw_source_id"],),
        ).fetchone()
    assert row["lead_id"] == result["lead_id"]
    assert len(lead["raw_sources"]) == len(db.get_raw_sources_for_lead(result["lead_id"], db_path=db_path))
