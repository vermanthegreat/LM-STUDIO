"""Regression tests for Shopify directory fallback (Shopvert full-page fixture)."""

from pathlib import Path

from extractor import deterministic_parse

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "shopvert_shopify.txt"
SHOPVERT_TEXT = FIXTURE_PATH.read_text(encoding="utf-8")

EXPECTED_SERVICES = [
    "Custom apps and integrations",
    "Checkout upgrade",
    "Ongoing website management",
    "Website audit and optimization strategy",
    "Headless commerce",
    "POS setup and migration",
]

EXPECTED_INDUSTRIES = [
    "Business to business (B2B)",
    "Clothing and fashion",
    "Health and beauty",
    "Services",
]

EXPECTED_FEATURED_WORK_TITLES = [
    "Feel22 Website & Mobile Application",
    "Lacoste",
]


def test_shopvert_fallback_extracts_contact_and_profile_fields():
    parsed = deterministic_parse("shopify_directory", SHOPVERT_TEXT)

    assert parsed["company_name"] == "Shopvert"
    assert parsed["company_name"] != "United Kingdom"
    assert parsed["website"] == "shopvert.com"
    assert parsed["company_email"] == "[info@shopvert.com](mailto:info@shopvert.com)"
    assert parsed["company_phone"] == "+971 52 700 94 95"
    assert parsed["partner_tier"] == "Service partner"
    assert parsed["plus_partner_signal"] is True
    assert parsed["rating"] == 5.0
    assert parsed["review_count"] == 58
    assert parsed["partner_since"] == "March 2019"
    assert parsed["primary_location"] == "Dubai, United Arab Emirates"

    for loc in ("United Arab Emirates", "Saudi Arabia", "United States", "United Kingdom"):
        assert loc in parsed["supported_locations"]

    assert parsed["languages"] == ["English", "French", "Arabic"]
    assert "About" not in parsed["languages"]

    assert parsed["description"].startswith(
        "Shopvert delivers enterprise-level Shopify expertise"
    )

    for svc in EXPECTED_SERVICES:
        assert svc in parsed["services"]
    for ind in EXPECTED_INDUSTRIES:
        assert ind in parsed["industries"]

    featured_titles = [item.get("title", "") for item in (parsed.get("featured_work") or [])]
    for title in EXPECTED_FEATURED_WORK_TITLES:
        assert any(title in ft for ft in featured_titles)

    assert not any("View featured work" in ft for ft in featured_titles)
    assert not any(
        "leading beauty retailer" in ft or "brand store redesign" in ft
        for ft in featured_titles
    )
