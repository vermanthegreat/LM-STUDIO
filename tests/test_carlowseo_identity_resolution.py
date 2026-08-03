"""Cross-source company identity regressions for the CarlowSEO fixtures."""

from pathlib import Path
from unittest.mock import patch

import db
from extractor import deterministic_parse, parse_and_save


FIXTURES = Path(__file__).parent / "fixtures"
SHOPIFY = (FIXTURES / "carlowseo_shopify.txt").read_text(encoding="utf-8")
LINKEDIN = (FIXTURES / "carlowseo_linkedin_company_people.txt").read_text(encoding="utf-8")
DISPLAY_NAME = "CarlowSEO - Helping Small & Midsize businesses grow for 13+ Years!"


def _ingest(source_type: str, text: str, db_path):
    with patch("extractor.extract_structured", return_value=(None, "")):
        return parse_and_save(source_type, text, db_path=db_path)


def test_shopify_profile_keeps_display_name_and_derives_only_safe_alias():
    parsed = deterministic_parse("shopify_directory", SHOPIFY)

    assert parsed["company_name"] == DISPLAY_NAME
    assert parsed["source_display_name"] == DISPLAY_NAME
    assert parsed["company_aliases"] == ["CarlowSEO"]
    assert parsed["company_website"] == "carlowseo.com"
    assert [item["name"] for item in parsed["company_alias_candidates"]] == ["CarlowSEO"]


def test_shopify_alias_derivation_does_not_blindly_truncate_punctuation():
    legal_name = SHOPIFY.replace(DISPLAY_NAME, "Smith - Jones LLC")
    brand_name = SHOPIFY.replace(DISPLAY_NAME, "X-Mozo")

    assert deterministic_parse("shopify_directory", legal_name)["company_aliases"] == []
    assert deterministic_parse("shopify_directory", brand_name)["company_aliases"] == []

    cropped = SHOPIFY.replace(DISPLAY_NAME, DISPLAY_NAME[1:], 1)
    assert deterministic_parse("shopify_directory", cropped)["company_aliases"] == []


def test_carlowseo_cross_source_match_source_precedence_and_idempotency(tmp_path):
    db_path = tmp_path / "carlowseo.db"
    db.init_db(db_path)
    shopify = _ingest("shopify_directory", SHOPIFY, db_path)
    before = db.get_lead(shopify["lead_id"], db_path=db_path)
    assert before is not None
    before_count = len(db.list_leads(db_path=db_path))
    before_score = before["fit_score"]

    first = _ingest("shopify_directory", LINKEDIN, db_path)
    after_first = db.get_lead(shopify["lead_id"], db_path=db_path)
    second = _ingest("shopify_directory", LINKEDIN, db_path)
    after_second = db.get_lead(shopify["lead_id"], db_path=db_path)

    assert after_first is not None and after_second is not None
    assert first["lead_id"] == second["lead_id"] == shopify["lead_id"]
    assert first["company_match_reason"] == "exact_source_alias"
    assert first["parsed"]["company_match_confidence"] == "high"
    assert len(db.list_leads(db_path=db_path)) == before_count
    assert after_second["company_name"] == DISPLAY_NAME
    assert after_second["website"] == "carlowseo.com"
    assert after_second["company_email"] == "experts@carlowseo.com"
    assert after_second["company_phone"] == "4842020762"
    assert after_second["primary_location"] == "Pottstown, United States"
    assert after_second["supported_locations"] == ["United States", "Canada"]
    assert after_second["languages"] == ["English"]
    assert after_second["rating"] == 5.0
    assert after_second["review_count"] == 413
    assert after_second["partner_since"] == "April 2011"
    assert after_second["partner_tier"] == "Plus Partner"
    assert after_second["plus_partner_signal"] is True
    assert after_second["services"] == before["services"]
    assert after_second["industries"] == before["industries"]
    assert after_second["description"] == before["description"]
    assert after_second["fit_score"] >= before_score
    candidates = db.list_person_candidates_for_lead(shopify["lead_id"], db_path=db_path)
    assert len(candidates) == 1
    person = candidates[0]
    assert person["name"] == "Trevor Carlow"
    assert person["title"] == (
        "Founder at CarlowSEO | E-Commerce & Shopify Specialist | "
        "Driving Growth Through Data-Driven Strategy"
    )
    assert person["profile_url"] is None
    assert person["is_decision_maker"] == 1
    assert person["role_type"] == "economic_buyer"
    assert first["parsed"]["people"][0]["source_type"] == "linkedin_company_people"
    assert first["parsed"]["people"][0]["source_company"] == "CarlowSEO"
    assert first["parsed"]["people"][0]["association_confidence"] == "explicit"
    linkedin_sources = [
        source for source in after_second["raw_sources"]
        if source["source_type"] == "linkedin_company"
    ]
    assert len(linkedin_sources) == 2
    assert all(source["source_type"] != "shopify_directory" for source in linkedin_sources)
    assert len(after_second["raw_sources"]) == 3
    assert after_second["interactions"] == []
    assert after_second["tasks"] == []


def test_carlowseo_linkedin_header_metadata_split():
    parsed = deterministic_parse("shopify_directory", LINKEDIN)

    assert parsed["classification"] == "linkedin_company_people"
    assert parsed["linkedin_industry"] == "Advertising Services"
    assert parsed["linkedin_location"] == "Pottstown, PENNSYLVANIA (PA)"
    assert parsed["linkedin_follower_count"] == 53
    assert parsed["linkedin_employee_range"] == "2-10"
    assert parsed["linkedin_associated_members"] == 1


def test_alias_ambiguity_fails_closed_and_preserves_unattached_source(tmp_path):
    db_path = tmp_path / "ambiguous.db"
    db.init_db(db_path)
    first, _ = db.upsert_lead({"company_name": "Shared - Helping stores grow"}, db_path=db_path)
    second, _ = db.upsert_lead({"company_name": "Shared | E-commerce growth experts"}, db_path=db_path)
    for lead in (first, second):
        db.create_raw_source(
            "shopify_directory",
            "valid source evidence",
            parsed_json={"company_aliases": ["CarlowSEO"]},
            lead_id=lead["id"],
            db_path=db_path,
        )
    before = {lead["id"]: db.get_lead(lead["id"], db_path=db_path) for lead in (first, second)}

    result = _ingest("shopify_directory", LINKEDIN, db_path)

    assert result["lead_id"] is None
    assert result["company_match_status"] == "needs_review"
    assert result["company_match_reason"] == "ambiguous_exact_source_alias"
    assert result["company_match_candidate_lead_ids"] == [first["id"], second["id"]]
    assert len(db.list_leads(db_path=db_path)) == 2
    assert all(db.get_lead(lead_id, db_path=db_path)["people"] == [] for lead_id in before)
    with db.get_conn(db_path) as conn:
        source = conn.execute(
            "SELECT lead_id, source_type, extraction_status FROM raw_sources WHERE id = ?",
            (result["raw_source_id"],),
        ).fetchone()
    assert source["lead_id"] is None
    assert source["source_type"] == "linkedin_company"
    assert source["extraction_status"] == "needs_review"


def test_partial_company_name_overlap_never_merges(tmp_path):
    db_path = tmp_path / "anti-substring.db"
    db.init_db(db_path)
    existing, _ = db.upsert_lead({"company_name": "CarlowSEO Holdings"}, db_path=db_path)

    result = _ingest("shopify_directory", LINKEDIN, db_path)

    assert result["lead_id"] != existing["id"]
    assert result["company_match_status"] == "unmatched"
    assert len(db.list_leads(db_path=db_path)) == 2
