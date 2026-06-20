"""Tests for contact/email ask routing and persistence."""

import db
from ask_router import answer_question
from extractor import parse_and_save


def _seed(db_path):
    db.init_db(db_path)
    lead, _ = db.upsert_lead(
        {
            "company_name": "Blinc Design",
            "website": "https://blinc.design",
            "company_email": "hello@blinc.design",
            "fit_score": 80,
        },
        db_path=db_path,
    )
    db.upsert_lead({"company_name": "No Email Co", "fit_score": 40}, db_path=db_path)
    db.add_person(
        lead["id"],
        {"name": "Jane Doe", "title": "CEO", "email": "jane@blinc.design"},
        db_path=db_path,
    )
    db.add_interaction(
        lead["id"],
        {"type": "email", "subject": "Intro", "summary": "Hello"},
        db_path=db_path,
    )


def test_contact_summary(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)
    result = answer_question("contact summary", use_llm=False, db_path=db_path)
    assert result["intent"] == "contact_summary"
    assert "Companies: 2" in result["answer"]
    assert "With any email" in result["answer"]
    assert result["data"]["with_any_email"] == 1


def test_list_emails(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)
    result = answer_question("print all emails from database", use_llm=False, db_path=db_path)
    assert result["intent"] == "list_emails"
    assert "hello@blinc.design" in result["answer"]
    assert "jane@blinc.design" in result["answer"]


def test_leads_without_email(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)
    result = answer_question("companies without email", use_llm=False, db_path=db_path)
    assert result["intent"] == "leads_without_email"
    assert "No Email Co" in result["answer"]


def test_parse_gmail_links_to_existing_company(tmp_path):
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    db.upsert_lead(
        {"company_name": "Blinc Design", "website": "https://blinc.design", "fit_score": 70},
        db_path=db_path,
    )
    raw = """From: Jane Doe <jane@blinc.design>
To: me@gmail.com
Subject: Follow up on Shopify project

Hi, checking in about the proposal.
"""
    result = parse_and_save("email", raw, db_path=db_path)
    assert result["lead_id"]
    lead = db.get_lead(result["lead_id"], db_path=db_path)
    assert lead["company_name"] == "Blinc Design"
    assert any(p.get("email") for p in lead["people"])
    assert lead["interactions"]
