"""Regression coverage for pasted LinkedIn company People pages."""

from pathlib import Path
from unittest.mock import patch

import db
from extractor import classify_linkedin_input, deterministic_parse, parse_and_save


FIXTURES = Path(__file__).parent / "fixtures"
LINKEDIN_URL = "https://www.linkedin.com/company/akuna-technologies/people/"
AKUNA_LINKEDIN = (FIXTURES / "akuna_linkedin_company_people.txt").read_text(encoding="utf-8")
AKUNA_SHOPIFY = (FIXTURES / "akuna_shopify.txt").read_text(encoding="utf-8")


def test_classifies_and_extracts_linkedin_company_people_fixture():
    assert AKUNA_LINKEDIN.startswith(
        "Akuna Technologies\nIT Services and IT Consulting CASTLE HILL, New South wale "
        "526 followers 11-50 employees"
    )
    assert classify_linkedin_input(
        AKUNA_LINKEDIN, None, "linkedin_company"
    ) == "linkedin_company_people"

    parsed = deterministic_parse("linkedin_company", AKUNA_LINKEDIN)

    assert parsed["classification"] == "linkedin_company_people"
    assert parsed["company_name"] == "Akuna Technologies"
    assert parsed["company_name"] != "0 notifications total"
    assert parsed["linkedin_company_url"] is None
    assert parsed["website"] is None
    assert parsed["description"] is None
    assert parsed["linkedin_industry"] == "IT Services and IT Consulting"
    assert parsed["linkedin_location"] == "CASTLE HILL, New South wale"
    assert parsed["linkedin_follower_count"] == 526
    assert parsed["linkedin_employee_range"] == "11-50"
    assert parsed["linkedin_associated_members"] == 31
    assert parsed["linkedin_function_distribution"] == [
        {"label": "Engineering", "count": 10},
        {"label": "Operations", "count": 6},
        {"label": "Arts and Design", "count": 4},
        {"label": "Marketing", "count": 3},
        {"label": "Business Development", "count": 3},
    ]
    assert parsed["services"] == []
    assert parsed["interaction"] is None
    assert not any(
        warning.endswith("company_name") for warning in parsed["extraction_warnings"]
    )


def test_full_linkedin_company_header_remains_compatible():
    full_clipboard = "Akuna Technologies logo\n" + AKUNA_LINKEDIN
    parsed = deterministic_parse("linkedin_company", full_clipboard, LINKEDIN_URL)

    assert parsed["classification"] == "linkedin_company_people"
    assert parsed["company_name"] == "Akuna Technologies"
    assert parsed["linkedin_industry"] == "IT Services and IT Consulting"
    assert parsed["linkedin_location"] == "CASTLE HILL, New South wale"
    assert len(parsed["people"]) == 9


def test_linkedin_classifier_covers_supported_and_unsupported_page_types():
    header = "Example Co logo\nExample Co\nHome\nAbout\nPosts\nJobs\nPeople"
    assert classify_linkedin_input(
        header, "https://www.linkedin.com/company/example/", "linkedin_company"
    ) == "linkedin_company_home"
    assert classify_linkedin_input(
        header, "https://www.linkedin.com/company/example/about/", "linkedin_company"
    ) == "linkedin_company_about"
    assert classify_linkedin_input(
        "Jane Doe\nExperience", "https://www.linkedin.com/in/jane-doe/", "website"
    ) == "linkedin_person_profile"
    assert classify_linkedin_input(
        "0 notifications total\nLinkedIn Corporation © 2026",
        None,
        "linkedin_company",
    ) == "unsupported_linkedin"

    unsupported = deterministic_parse(
        "linkedin_company", "0 notifications total\nLinkedIn Corporation © 2026"
    )
    assert unsupported["company_name"] is None
    assert unsupported["website"] is None
    assert unsupported["description"] is None
    assert unsupported["services"] == []


def test_people_cards_are_bounded_normalized_and_named_only():
    people = deterministic_parse(
        "linkedin_company", AKUNA_LINKEDIN, LINKEDIN_URL
    )["people"]
    by_name = {person["name"]: person for person in people}

    assert len(people) == 9
    assert by_name["Praveen Gowda"]["title"] == "CEO at Akuna Technologies"
    assert by_name["Prashanth E"]["title"] == (
        "Front End Developer | React | Vue JS | ExtJS | Javascript | HTML5 | CSS"
    )
    assert all("is open to work" not in person["name"].casefold() for person in people)
    assert "LinkedIn Member" not in by_name
    assert "Nikola Milic" not in by_name
    assert "Yuval Tsabari" not in by_name
    assert all(person["linkedin_url"] is None for person in people)
    assert all(person["source_type"] == "linkedin_company_people" for person in people)
    assert all(person["source_company"] == "Akuna Technologies" for person in people)
    assert all(person["association_confidence"] == "explicit" for person in people)


def test_linkedin_merge_preserves_shopify_canonical_fields(tmp_path):
    db_path = tmp_path / "linkedin-precedence.db"
    db.init_db(db_path)
    with patch("extractor.extract_structured", return_value=(None, "")):
        shopify = parse_and_save(
            "shopify_directory",
            AKUNA_SHOPIFY,
            source_url="https://www.shopify.com/partners/directory/partner/shopifytech",
            db_path=db_path,
        )
    before = db.get_lead(shopify["lead_id"], db_path=db_path)
    assert before is not None

    misleading_llm = {
        "company_name": "0 notifications total",
        "website": LINKEDIN_URL,
        "services": ["Seo", "Catalog", "Product Management"],
        "locations": [],
        "industries": [],
        "description": "Skip to search Skip to main content",
        "people": [],
        "interaction": None,
        "confidence": 0.4,
    }
    with patch("extractor.extract_structured", return_value=(misleading_llm, "llm-json")):
        result = parse_and_save(
            "linkedin_company",
            AKUNA_LINKEDIN,
            source_url=LINKEDIN_URL,
            db_path=db_path,
        )

    after = db.get_lead(shopify["lead_id"], db_path=db_path)
    assert after is not None
    assert result["lead_id"] == shopify["lead_id"]
    assert result["extraction_status"] == "ok"
    assert result["parsed"]["classification"] == "linkedin_company_people"
    assert result["people_count"] == 9
    assert result["interaction_id"] is None
    assert after["website"] == "akunatech.com"
    assert after["description"] == before["description"]
    assert after["services"] == before["services"]
    assert after["partner_tier"] == before["partner_tier"]
    assert after["rating"] == before["rating"]
    assert after["review_count"] == before["review_count"]
    assert not {"Seo", "Catalog", "Product Management"} & set(after["services"])
    assert len(after["interactions"]) == len(before["interactions"])
    assert all(person["name"] != "LinkedIn Member" for person in after["people"])
    linked_source = next(
        source for source in after["raw_sources"] if source["source_url"] == LINKEDIN_URL
    )
    assert linked_source["source_url"] == LINKEDIN_URL
    assert linked_source["parsed_json"]["linkedin_company_url"] == LINKEDIN_URL


def test_generic_non_linkedin_fallback_is_unchanged():
    parsed = deterministic_parse("website", "Example Co\nhttps://example.com\nSEO")
    assert parsed["company_name"] == "Example Co"
    assert parsed["website"] == "https://example.com"
    assert "Seo" in parsed["services"]


def test_unsupported_linkedin_does_not_fall_back_or_overwrite_attached_lead(tmp_path):
    db_path = tmp_path / "unsupported-linkedin.db"
    db.init_db(db_path)
    with patch("extractor.extract_structured", return_value=(None, "")):
        shopify = parse_and_save(
            "shopify_directory",
            AKUNA_SHOPIFY,
            source_url="https://www.shopify.com/partners/directory/partner/shopifytech",
            db_path=db_path,
        )
        before = db.get_lead(shopify["lead_id"], db_path=db_path)
        result = parse_and_save(
            "linkedin_company",
            "0 notifications total\nLinkedIn Corporation © 2026",
            attach_to_lead_id=shopify["lead_id"],
            db_path=db_path,
        )
        after = db.get_lead(shopify["lead_id"], db_path=db_path)

    assert before is not None and after is not None
    assert result["extraction_status"] == "needs_review"
    assert result["parsed"]["classification"] == "unsupported_linkedin"
    assert result["parsed"]["company_name"] is None
    assert after["company_name"] == before["company_name"]
    assert after["website"] == before["website"]
    assert after["services"] == before["services"]
    assert after["description"] == before["description"]
    assert len(after["people"]) == len(before["people"]) == 0


def test_repeated_people_ingestion_is_idempotent_and_keeps_raw_history(tmp_path):
    db_path = tmp_path / "linkedin-idempotency.db"
    db.init_db(db_path)
    with patch("extractor.extract_structured", return_value=(None, "")):
        shopify = parse_and_save(
            "shopify_directory",
            AKUNA_SHOPIFY,
            source_url="https://www.shopify.com/partners/directory/partner/shopifytech",
            db_path=db_path,
        )
        before = db.get_lead(shopify["lead_id"], db_path=db_path)
        first = parse_and_save("linkedin_company", AKUNA_LINKEDIN, db_path=db_path)
        after_first = db.get_lead(shopify["lead_id"], db_path=db_path)
        second = parse_and_save("linkedin_company", AKUNA_LINKEDIN, db_path=db_path)
        after_second = db.get_lead(shopify["lead_id"], db_path=db_path)

    assert before is not None and after_first is not None and after_second is not None
    assert first["lead_id"] == second["lead_id"] == shopify["lead_id"]
    assert len(after_first["people"]) == len(after_second["people"]) == 9
    normalized = [db.normalize_name(person["name"]) for person in after_second["people"]]
    assert len(normalized) == len(set(normalized))
    assert len(after_second["raw_sources"]) == len(before["raw_sources"]) + 2
    assert len(after_second["interactions"]) == len(before["interactions"]) == 0
    assert len(after_second["tasks"]) == len(before["tasks"]) == 0
    assert after_second["website"] == "akunatech.com"
    assert after_second["services"] == before["services"]
    assert all(
        person["source_company"] == "Akuna Technologies"
        for person in second["parsed"]["people"]
    )


def test_person_name_identity_updates_title_and_preserves_stronger_fields(tmp_path):
    db_path = tmp_path / "person-upsert.db"
    db.init_db(db_path)
    lead, _ = db.upsert_lead({"company_name": "Example Co"}, db_path=db_path)
    first = db.add_person(
        lead["id"],
        {
            "name": "Praveen Gowda",
            "title": "Chief Executive Officer",
            "email": "praveen@example.com",
            "linkedin_url": "https://www.linkedin.com/in/praveen-gowda/",
            "is_decision_maker": True,
            "is_relevant_contact": True,
        },
        db_path=db_path,
    )
    second = db.add_person(
        lead["id"],
        {
            "name": "  praveen   gowda ",
            "title": "CEO at Example Co",
            "email": None,
            "linkedin_url": None,
            "is_decision_maker": False,
            "is_relevant_contact": False,
        },
        db_path=db_path,
    )
    saved = db.get_lead(lead["id"], db_path=db_path)

    assert saved is not None
    assert first["id"] == second["id"]
    assert len(saved["people"]) == 1
    assert second["title"] == "CEO at Example Co"
    assert second["email"] == "praveen@example.com"
    assert second["linkedin_url"] == "https://www.linkedin.com/in/praveen-gowda"
    assert second["is_decision_maker"] == 1
    assert second["is_relevant_contact"] == 1
