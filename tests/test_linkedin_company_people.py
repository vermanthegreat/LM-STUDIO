"""Regression coverage for pasted LinkedIn company People pages."""

from pathlib import Path
from unittest.mock import patch

import db
import pytest
from extractor import classify_linkedin_input, deterministic_parse, parse_and_save
from repositories.sqlite_store import SqliteContactStore


FIXTURES = Path(__file__).parent / "fixtures"
LINKEDIN_URL = "https://www.linkedin.com/company/akuna-technologies/people/"
AKUNA_LINKEDIN = (FIXTURES / "akuna_linkedin_company_people.txt").read_text(encoding="utf-8")
AKUNA_SHOPIFY = (FIXTURES / "akuna_shopify.txt").read_text(encoding="utf-8")


def _synthetic_people_page(
    *,
    company: str = "Example Co",
    name: str = "Jane Doe",
    title: str = "Founder",
    email: str | None = None,
    profile_url: str | None = None,
    website: str | None = None,
) -> str:
    evidence = [
        f"{company} logo",
        company,
        "Home",
        "About",
        "Posts",
        "Jobs",
        "People",
    ]
    if website:
        evidence.append(website)
    evidence.extend(
        [
            "People you may know",
            name,
            f"{name} 2nd degree connection",
            title,
        ]
    )
    if email:
        evidence.append(email)
    if profile_url:
        evidence.append(profile_url)
    evidence.extend(["Following", "About"])
    return "\n".join(evidence)


def _ingest_synthetic_people_page(text: str, db_path, source_url: str):
    with patch("extractor.extract_structured", return_value=(None, "")):
        return parse_and_save(
            "linkedin_company",
            text,
            source_url=source_url,
            db_path=db_path,
        )


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
    candidates = db.list_person_candidates_for_lead(shopify["lead_id"], db_path=db_path)
    assert result["people_count"] == len(candidates) == 9
    assert all(candidate["name"] != "LinkedIn Member" for candidate in candidates)
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
        first_candidates = db.list_person_candidates_for_lead(shopify["lead_id"], db_path=db_path)
        second = parse_and_save("linkedin_company", AKUNA_LINKEDIN, db_path=db_path)
        after_second = db.get_lead(shopify["lead_id"], db_path=db_path)

    assert before is not None and after_first is not None and after_second is not None
    assert first["lead_id"] == second["lead_id"] == shopify["lead_id"]
    first_ids = [candidate["id"] for candidate in first_candidates]
    second_candidates = db.list_person_candidates_for_lead(shopify["lead_id"], db_path=db_path)
    assert len(first_candidates) == len(second_candidates) == 9
    assert [candidate["id"] for candidate in second_candidates] == first_ids
    assert all(candidate["version"] == 1 and candidate["status"] == "needs_review" for candidate in second_candidates)
    normalized = [candidate["normalized_name"] for candidate in second_candidates]
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


def test_people_ingestion_persists_explicit_contact_candidates_only(tmp_path):
    db_path = tmp_path / "linkedin-explicit-contacts.db"
    db.init_db(db_path)
    lead, _ = db.upsert_lead(
        {"company_name": "Example Co", "website": "example.com"}, db_path=db_path
    )
    source_url = "https://www.linkedin.com/company/example/people/"
    page = _synthetic_people_page(
        email="jane@example.com",
        profile_url="https://www.linkedin.com/in/jane-doe/",
        website="https://example.com",
    )

    result = _ingest_synthetic_people_page(page, db_path, source_url)

    candidates = db.list_person_candidates_for_lead(lead["id"], db_path=db_path)
    contacts = db.list_contact_candidates_for_lead(lead["id"], db_path=db_path)
    saved = db.get_lead(lead["id"], db_path=db_path)
    assert result["lead_id"] == lead["id"]
    assert len(candidates) == 1
    person = candidates[0]
    assert person["raw_source_id"] == result["raw_source_id"]
    assert person["source_type"] == "linkedin_company_people"
    assert person["source_url"] == source_url
    assert person["name"] == "Jane Doe"
    assert person["normalized_name"] == "jane doe"
    assert person["title"] == "Founder"
    assert person["role_type"] == "economic_buyer"
    assert person["is_decision_maker"] == 1
    assert person["profile_url"] == "https://www.linkedin.com/in/jane-doe"
    assert person["discovery_method"] == "deterministic_parser"
    assert person["status"] == "needs_review"
    assert person["version"] == 1
    assert len(contacts) == 2
    by_kind = {contact["kind"]: contact for contact in contacts}
    assert by_kind["email"]["normalized_value"] == "jane@example.com"
    assert by_kind["linkedin"]["normalized_value"] == (
        "https://www.linkedin.com/in/jane-doe"
    )
    for contact in contacts:
        assert contact["person_candidate_id"] == person["id"]
        assert contact["raw_source_id"] == result["raw_source_id"]
        assert contact["source_url"] == source_url
        assert contact["evidence_basis"] == "source_confirmed"
        assert contact["verification_status"] == "source_confirmed"
        assert contact["verification_status"] != "verified"
    assert saved["people"] == []
    assert saved["company_email"] is None


def test_people_ingestion_does_not_guess_missing_contacts(tmp_path):
    db_path = tmp_path / "linkedin-no-guesses.db"
    db.init_db(db_path)
    lead, _ = db.upsert_lead(
        {"company_name": "Example Co", "website": "example.com"}, db_path=db_path
    )
    page = _synthetic_people_page(website="https://example.com")

    _ingest_synthetic_people_page(
        page, db_path, "https://www.linkedin.com/company/example/people/"
    )

    candidates = db.list_person_candidates_for_lead(lead["id"], db_path=db_path)
    contacts = db.list_contact_candidates_for_lead(lead["id"], db_path=db_path)
    saved = db.get_lead(lead["id"], db_path=db_path)
    assert len(candidates) == 1
    assert contacts == []
    assert saved["people"] == []
    assert saved["company_email"] is None


def test_changed_people_source_keeps_separate_candidate_evidence(tmp_path):
    db_path = tmp_path / "linkedin-source-evidence.db"
    db.init_db(db_path)
    lead, _ = db.upsert_lead({"company_name": "Example Co"}, db_path=db_path)
    source_url = "https://www.linkedin.com/company/example/people/"

    first = _ingest_synthetic_people_page(
        _synthetic_people_page(title="Founder"), db_path, source_url
    )
    second = _ingest_synthetic_people_page(
        _synthetic_people_page(title="Chief Executive Officer") + "\nUpdated source evidence",
        db_path,
        source_url,
    )

    candidates = db.list_person_candidates_for_lead(lead["id"], db_path=db_path)
    saved = db.get_lead(lead["id"], db_path=db_path)
    assert first["raw_source_id"] != second["raw_source_id"]
    assert len(saved["raw_sources"]) == 2
    assert len(candidates) == 2
    assert {candidate["raw_source_id"] for candidate in candidates} == {
        first["raw_source_id"],
        second["raw_source_id"],
    }
    by_source = {candidate["raw_source_id"]: candidate for candidate in candidates}
    assert by_source[first["raw_source_id"]]["title"] == "Founder"
    assert by_source[second["raw_source_id"]]["title"] == "Chief Executive Officer"
    assert saved["people"] == []


def test_people_candidate_transaction_rolls_back_all_intake_records(tmp_path):
    db_path = tmp_path / "linkedin-candidate-rollback.db"
    db.init_db(db_path)
    lead, _ = db.upsert_lead(
        {
            "company_name": "Example Co",
            "company_email": "company@example.com",
            "company_phone": "555-0100",
            "website": "example.com",
        },
        db_path=db_path,
    )
    canonical = db.add_person(
        lead["id"],
        {"name": "Existing Person", "title": "Owner", "email": "owner@example.com"},
        db_path=db_path,
    )
    interaction = db.add_interaction(
        lead["id"], {"type": "note", "summary": "Existing interaction"}, db_path=db_path
    )
    task = db.add_task(
        lead["id"], {"title": "Existing task", "due_date": "2026-08-04"}, db_path=db_path
    )
    before = db.get_lead(lead["id"], db_path=db_path)
    page = _synthetic_people_page(email="jane@example.com")

    with patch.object(
        SqliteContactStore,
        "create_or_reuse_contact_candidate",
        side_effect=RuntimeError("forced candidate failure"),
    ):
        with pytest.raises(RuntimeError, match="forced candidate failure"):
            _ingest_synthetic_people_page(
                page,
                db_path,
                "https://www.linkedin.com/company/example/people/",
            )

    after = db.get_lead(lead["id"], db_path=db_path)
    assert after["raw_sources"] == before["raw_sources"]
    assert db.list_person_candidates_for_lead(lead["id"], db_path=db_path) == []
    assert db.list_contact_candidates_for_lead(lead["id"], db_path=db_path) == []
    assert after["people"] == before["people"]
    assert after["tasks"] == before["tasks"]
    assert after["interactions"] == before["interactions"]
    assert after["company_email"] == before["company_email"]
    assert after["company_phone"] == before["company_phone"]
    assert after["website"] == before["website"]
    assert after["people"][0]["id"] == canonical["id"]
    assert after["tasks"][0]["id"] == task["id"]
    assert after["interactions"][0]["id"] == interaction["id"]
    assert db.get_lead(lead["id"], db_path=db_path)["company_name"] == "Example Co"


def test_people_ingestion_isolates_populated_canonical_records(tmp_path):
    db_path = tmp_path / "linkedin-canonical-isolation.db"
    db.init_db(db_path)
    lead, _ = db.upsert_lead(
        {
            "company_name": "Example Co",
            "company_email": "company@example.com",
            "company_phone": "555-0100",
            "website": "example.com",
        },
        db_path=db_path,
    )
    canonical = db.add_person(
        lead["id"],
        {
            "name": "Existing Person",
            "title": "Managing Director",
            "email": "owner@example.com",
            "linkedin_url": "https://www.linkedin.com/in/existing-person",
        },
        db_path=db_path,
    )
    db.add_task(lead["id"], {"title": "Existing task", "due_date": "2026-08-04"}, db_path=db_path)
    db.add_interaction(
        lead["id"], {"type": "note", "summary": "Existing interaction"}, db_path=db_path
    )
    before = db.get_lead(lead["id"], db_path=db_path)

    result = _ingest_synthetic_people_page(
        _synthetic_people_page(),
        db_path,
        "https://www.linkedin.com/company/example/people/",
    )

    after = db.get_lead(lead["id"], db_path=db_path)
    candidates = db.list_person_candidates_for_lead(lead["id"], db_path=db_path)
    assert result["lead_id"] == lead["id"]
    assert len(candidates) == 1
    assert len(after["people"]) == len(before["people"]) == 1
    assert after["people"] == before["people"]
    assert after["people"][0]["id"] == canonical["id"]
    for field in ("company_email", "company_phone", "website"):
        assert after[field] == before[field]
    assert after["tasks"] == before["tasks"]
    assert after["interactions"] == before["interactions"]
    assert len(after["raw_sources"]) == len(before["raw_sources"]) + 1
    assert db.list_contact_candidates_for_lead(lead["id"], db_path=db_path) == []
