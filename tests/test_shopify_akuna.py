"""Regression coverage for complete Shopify individual-profile ingestion."""

from pathlib import Path
from unittest.mock import patch

import db
from extractor import deterministic_parse, parse_and_save


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "akuna_shopify.txt"
AKUNA_TEXT = FIXTURE_PATH.read_text(encoding="utf-8")

EXPECTED_SERVICES = [
    "Store build or redesign",
    "Theme customization",
    "Store migration",
    "POS setup and migration",
    "Product and collection setup",
    "Troubleshooting",
    "Ongoing website management",
    "Checkout upgrade",
    "Store settings configuration",
    "Analytics and tracking",
    "Banner ads",
]

EXPECTED_INDUSTRIES = [
    "Clothing and fashion",
    "Food and drink",
    "Health and beauty",
    "Jewelry and accessories",
]


def test_akuna_deterministic_profile_extracts_all_explicit_fields():
    parsed = deterministic_parse("shopify_directory", AKUNA_TEXT)

    assert parsed["company_name"] == "Akuna Technologies"
    assert parsed["company_website"] == "akunatech.com"
    assert parsed["website"] == "akunatech.com"
    assert parsed["company_email"] == "sales@akunatech.com"
    assert parsed["company_phone"] is None
    assert parsed["rating"] == 5.0
    assert parsed["review_count"] == 1569
    assert parsed["partner_since"] == "February 2015"
    assert parsed["partner_tier"] == "Plus Partner"
    assert parsed["plus_partner_signal"] is True
    assert parsed["primary_location"] == "Sydney, Australia"
    assert parsed["supported_locations"] == [
        "Australia", "United States", "Canada", "United Kingdom",
    ]
    assert parsed["supported_locations_additional_count"] == 51
    assert parsed["languages"] == ["English", "Spanish", "German"]
    assert parsed["languages_additional_count"] == 5
    assert parsed["services"] == EXPECTED_SERVICES
    assert parsed["specialized_services"] == EXPECTED_SERVICES[:5]
    assert parsed["other_services"] == EXPECTED_SERVICES[5:]
    assert parsed["industries"] == EXPECTED_INDUSTRIES
    assert parsed["people"] == []
    assert parsed["interaction"] is None
    assert parsed["confidence"] >= 0.9


def test_akuna_profile_overrides_incomplete_llm_and_saves_only_source(tmp_path):
    db_path = tmp_path / "akuna.db"
    db.init_db(db_path)
    incomplete_llm = {
        "company_name": "Akuna Technologies",
        "website": "akunatech.com",
        "partner_tier": "Service partner",
        "services": ["Store migration"],
        "locations": ["Sydney, Australia"],
        "industries": [],
        "description": "Concise model summary.",
        "people": [{"name": "Yash", "title": None, "linkedin_url": None, "department": None}],
        "interaction": {
            "subject": "Shopify Partner Directory Listing",
            "summary": "Synthetic directory interaction.",
            "reply_needed": False,
            "deadline": None,
            "next_action": None,
        },
        "confidence": 0.95,
    }

    with patch("extractor.extract_structured", return_value=(incomplete_llm, "llm-json")):
        result = parse_and_save(
            "shopify_directory",
            AKUNA_TEXT,
            source_url="https://www.shopify.com/partners/directory/partner/shopifytech",
            db_path=db_path,
        )

    assert result["extraction_status"] == "ok"
    assert result["confidence"] >= 0.9
    assert result["people_count"] == 0
    assert result["interaction_id"] is None

    lead = db.get_lead(result["lead_id"], db_path=db_path)
    assert lead is not None
    assert lead["company_name"] == "Akuna Technologies"
    assert lead["website"] == "akunatech.com"
    assert lead["company_email"] == "sales@akunatech.com"
    assert lead["company_phone"] is None
    assert lead["rating"] == 5.0
    assert lead["review_count"] == 1569
    assert lead["partner_since"] == "February 2015"
    assert lead["partner_tier"] == "Plus Partner"
    assert lead["plus_partner_signal"] is True
    assert lead["primary_location"] == "Sydney, Australia"
    assert lead["supported_locations"] == [
        "Australia", "United States", "Canada", "United Kingdom",
    ]
    assert lead["languages"] == ["English", "Spanish", "German"]
    assert lead["services"] == EXPECTED_SERVICES
    assert lead["industries"] == EXPECTED_INDUSTRIES
    assert lead["description"] == "Concise model summary."
    assert lead["people"] == []
    assert lead["interactions"] == []
    assert len(lead["raw_sources"]) == 1
    parsed_source = lead["raw_sources"][0]["parsed_json"]
    assert parsed_source["supported_locations_additional_count"] == 51
    assert parsed_source["languages_additional_count"] == 5
    assert parsed_source["people"] == []
    assert parsed_source["interaction"] is None
