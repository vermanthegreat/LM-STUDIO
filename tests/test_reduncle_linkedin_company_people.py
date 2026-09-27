"""Regressions for duplicate LinkedIn company People-page headers."""

from pathlib import Path
from unittest.mock import patch

import db
from extractor import classify_linkedin_input, deterministic_parse, parse_and_save


FIXTURE = (
    Path(__file__).parent / "fixtures" / "reduncle_linkedin_company_people.txt"
).read_text(encoding="utf-8")
PERSONAL_URL = "https://www.linkedin.com/in/jdcolomer/"
COMPANY_URL = "https://www.linkedin.com/company/reduncle/people/"


def _ingest(text: str, db_path, source_url: str, attach_to_lead_id=None):
    with patch("extractor.extract_structured", return_value=(None, "")):
        return parse_and_save(
            "linkedin_company",
            text,
            source_url=source_url,
            attach_to_lead_id=attach_to_lead_id,
            db_path=db_path,
        )


def test_duplicate_header_selects_complete_candidate_tagline_and_metadata():
    assert classify_linkedin_input(
        FIXTURE, PERSONAL_URL, "linkedin_company"
    ) == "linkedin_company_people"

    parsed = deterministic_parse("linkedin_company", FIXTURE, PERSONAL_URL)

    assert parsed["classification"] == "linkedin_company_people"
    assert parsed["company_name"] == "Reduncle | Shopify Plus Partners"
    assert parsed["linkedin_tagline"] == "Shopify Experts"
    assert parsed["linkedin_industry"] == "Software Development"
    assert parsed["linkedin_location"] == "Madrid, Madrid"
    assert parsed["linkedin_follower_count"] == 669
    assert parsed["linkedin_employee_range"] == "2-10"
    assert parsed["linkedin_header_metadata_raw"] == (
        "Software Development Madrid, Madrid 669 followers 2-10 employees"
    )
    assert parsed["linkedin_associated_members"] == 2
    assert parsed["services"] == []
    assert "linkedin_source_url_classification_mismatch" in parsed["extraction_warnings"]


def test_ambiguous_header_split_retains_suffix_metadata_and_warns():
    text = FIXTURE.replace(
        "Software Development Madrid, Madrid 669 followers 2-10 employees",
        "Unknown Field Somewhere nearby 669 followers 2-10 employees",
    )

    parsed = deterministic_parse("linkedin_company", text, COMPANY_URL)

    assert parsed["linkedin_industry"] is None
    assert parsed["linkedin_location"] is None
    assert parsed["linkedin_follower_count"] == 669
    assert parsed["linkedin_employee_range"] == "2-10"
    assert parsed["linkedin_header_metadata_raw"] == (
        "Unknown Field Somewhere nearby 669 followers 2-10 employees"
    )
    assert "linkedin_header_metadata_ambiguous_split" in parsed["extraction_warnings"]


def test_locale_personal_url_shapes_are_rejected_for_company_content():
    for source_url in (
        "https://es.linkedin.com/in/jdcolomer/",
        "https://uk.linkedin.com/pub/jd-colomer/1/2/3/",
    ):
        parsed = deterministic_parse("linkedin_company", FIXTURE, source_url)
        assert parsed["classification"] == "linkedin_company_people"
        assert parsed["linkedin_company_url"] is None
        assert parsed["source_url"] is None
        assert "linkedin_source_url_classification_mismatch" in parsed["extraction_warnings"]


def test_people_boundary_and_personal_url_mismatch_persistence(tmp_path):
    db_path = tmp_path / "reduncle-mismatch.db"
    db.init_db(db_path)

    result = _ingest(FIXTURE, db_path, PERSONAL_URL)
    saved = db.get_lead(result["lead_id"], db_path=db_path)

    assert saved is not None
    assert result["company_match_reason"] == "no_reliable_company_match"
    assert result["parsed"]["linkedin_company_url"] is None
    assert result["parsed"]["source_url"] is None
    assert saved["website"] is None
    candidates = db.list_person_candidates_for_lead(result["lead_id"], db_path=db_path)
    assert [person["name"] for person in candidates] == [
        "Daniel Colomer",
        "Sergio Ivorra Puig",
    ]
    assert all(person["profile_url"] is None for person in candidates)
    assert all(
        person["source_company"] == "Reduncle | Shopify Plus Partners"
        for person in result["parsed"]["people"]
    )
    assert {"Sam Wright", "Gary Feuerstein", "Nikola Milic", "LinkedIn Member"}.isdisjoint(
        person["name"] for person in candidates
    )
    source = saved["raw_sources"][0]
    assert source["source_type"] == "linkedin_company"
    assert source["source_url"] is None
    assert source["parsed_json"]["classification"] == "linkedin_company_people"


def test_correct_company_url_is_preserved_with_identical_company_parsing(tmp_path):
    db_path = tmp_path / "reduncle-company-url.db"
    db.init_db(db_path)

    mismatch = deterministic_parse("linkedin_company", FIXTURE, PERSONAL_URL)
    result = _ingest(FIXTURE, db_path, COMPANY_URL)
    saved = db.get_lead(result["lead_id"], db_path=db_path)

    assert saved is not None
    assert result["parsed"]["linkedin_company_url"] == COMPANY_URL
    assert result["parsed"]["source_url"] == COMPANY_URL
    assert saved["raw_sources"][0]["source_url"] == COMPANY_URL
    assert saved["raw_sources"][0]["source_type"] == "linkedin_company"
    for key in (
        "classification",
        "company_name",
        "linkedin_tagline",
        "linkedin_industry",
        "linkedin_location",
        "linkedin_follower_count",
        "linkedin_employee_range",
        "linkedin_associated_members",
        "people",
    ):
        assert result["parsed"][key] == mismatch[key]


def test_repeated_ingestion_is_idempotent_and_preserves_source_precedence(tmp_path):
    db_path = tmp_path / "reduncle-idempotency.db"
    db.init_db(db_path)
    lead, _ = db.upsert_lead(
        {
            "company_name": "Reduncle | Shopify Plus Partners",
            "website": "reduncle.es",
            "services": ["Store migration"],
            "locations": ["Spain"],
            "industries": ["E-commerce"],
            "description": "Existing canonical description",
            "confidence": 0.99,
        },
        db_path=db_path,
    )

    first = _ingest(FIXTURE, db_path, PERSONAL_URL)
    second = _ingest(FIXTURE, db_path, PERSONAL_URL)
    saved = db.get_lead(lead["id"], db_path=db_path)

    assert saved is not None
    assert first["lead_id"] == second["lead_id"] == lead["id"]
    assert first["company_match_reason"] == second["company_match_reason"] == "exact_canonical_name"
    candidates = db.list_person_candidates_for_lead(lead["id"], db_path=db_path)
    assert len(candidates) == 2
    normalized = [person["normalized_name"] for person in candidates]
    assert len(normalized) == len(set(normalized))
    assert len(saved["raw_sources"]) == 2
    assert saved["interactions"] == []
    assert saved["tasks"] == []
    assert saved["website"] == "reduncle.es"
    assert saved["services"] == ["Store migration"]
    assert saved["locations"] == ["Spain"]
    assert saved["industries"] == ["E-commerce"]
    assert saved["description"] == "Existing canonical description"
    assert all(source["source_url"] is None for source in saved["raw_sources"])
