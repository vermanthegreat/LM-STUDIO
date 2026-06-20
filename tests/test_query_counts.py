"""Tests for database query counts and intent routing."""

import json
from unittest.mock import patch

import db
from ask_router import (
    AskIntent,
    answer_question,
    deterministic_ask_intent,
    parse_llm_intent_payload,
    route_question,
)


def _seed(db_path):
    db.init_db(db_path)
    l1, _ = db.upsert_lead(
        {"company_name": "High Fit Co", "fit_score": 90, "status": "qualified"},
        db_path=db_path,
    )
    l2, _ = db.upsert_lead(
        {"company_name": "No Contacts Co", "fit_score": 50},
        db_path=db_path,
    )
    db.upsert_lead({"company_name": "Closed Co", "status": "closed"}, db_path=db_path)
    db.add_person(l1["id"], {"name": "CEO Person", "title": "CEO", "is_decision_maker": 1}, db_path=db_path)
    db.add_task(
        l1["id"],
        {"title": "Follow up", "due_date": "2020-01-01", "status": "open"},
        db_path=db_path,
    )


def test_count_potential_clients(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)
    assert db.count_potential_clients(db_path=db_path) == 2


def test_query_count_hr(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)
    result = route_question("koliko imamo potencijalnih klijenata?", db_path=db_path, use_llm=False)
    assert result["intent"] == "count_leads"
    assert result["data"]["count"] == 2
    assert "2" in result["answer"]


def test_query_top_leads_deterministic(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)
    for question in ("show top leads", "top leads", "show best leads", "best leads"):
        result = route_question(question, db_path=db_path, use_llm=False)
        assert result["intent"] == "top_leads", f"Failed for {question}"
        assert "Nisam siguran" not in result["answer"], f"Unknown fallback in answer for {question}"
        assert "High Fit Co" in result["answer"], f"Expected lead in answer for {question}"
        assert result["data"]["leads"]


def test_query_top_leads_deterministic_croatian(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)
    result = route_question("prikazi top leadove", db_path=db_path, use_llm=False)
    assert result["intent"] == "top_leads"
    assert "Nisam siguran" not in result["answer"]


def test_query_without_contacts(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)
    result = route_question("show leads without contacts", db_path=db_path, use_llm=False)
    assert result["intent"] == "leads_without_contacts"
    assert "No Contacts Co" in result["answer"]


def test_query_followups_due(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)
    result = route_question("show follow-ups due", db_path=db_path, use_llm=False)
    assert result["intent"] == "followups_due"
    assert "Follow-ups due:" in result["answer"]


def test_query_summarize_company(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)
    l, _ = db.upsert_lead(
        {"company_name": "TestCorp", "fit_score": 70, "description": "A test company"},
        db_path=db_path,
    )
    result = route_question("summarize company TestCorp", db_path=db_path, use_llm=False)
    assert result["intent"] == "summarize_company"
    assert "TestCorp" in result["answer"]


def test_deterministic_ask_intent_count_leads() -> None:
    q = deterministic_ask_intent("show top leads")
    assert q is not None
    assert q.intent == "top_leads"
    assert q.confidence == 1.0


def test_deterministic_ask_intent_unknown() -> None:
    q = deterministic_ask_intent("some random question")
    assert q is None


def test_parse_llm_intent_valid() -> None:
    payload = json.dumps(
        {
            "intent": "top_leads",
            "company": None,
            "limit": 10,
            "filters": {},
            "confidence": 0.95,
        }
    )
    intent = parse_llm_intent_payload(payload)
    assert intent.intent == "top_leads"
    assert intent.confidence == 0.95
    assert intent.limit == 10


def test_parse_llm_intent_invalid_json() -> None:
    intent = parse_llm_intent_payload("not json at all {{{{")
    assert intent.intent == "unknown"


def test_parse_llm_intent_unknown_intent() -> None:
    payload = json.dumps({"intent": "garbage_intent", "confidence": 0.9})
    intent = parse_llm_intent_payload(payload)
    assert intent.intent == "unknown"


def test_parse_llm_intent_low_confidence() -> None:
    payload = json.dumps({"intent": "top_leads", "confidence": 0.5})
    intent = parse_llm_intent_payload(payload)
    assert intent.intent == "unknown"


def test_parse_llm_intent_limit_clamp() -> None:
    payload = json.dumps({"intent": "top_leads", "limit": 0})
    intent = parse_llm_intent_payload(payload)
    assert intent.limit == 1

    payload = json.dumps({"intent": "top_leads", "limit": 30})
    intent = parse_llm_intent_payload(payload)
    assert intent.limit == 25


def test_parse_llm_intent_null_fields() -> None:
    payload = json.dumps({"intent": "count_leads"})
    intent = parse_llm_intent_payload(payload)
    assert intent.intent == "count_leads"
    assert intent.company == ""


def test_route_llm_fallback_unknown_question() -> None:
    mock_payload = json.dumps({"intent": "top_leads", "confidence": 0.9, "limit": 10})

    with patch("ask_router.call_lmstudio_for_text", return_value=mock_payload):
        result = route_question("I have a random question")
        assert result["intent"] == "top_leads"
        assert "leads" in result["data"]


def test_route_llm_fallback_invalid_json() -> None:
    with patch("ask_router.call_lmstudio_for_text", return_value="not json {{{{"):
        result = route_question("unknown question")
        assert result is not None


def test_route_llm_fallback_unknown_intent() -> None:
    mock_payload = json.dumps({"intent": "count_leads", "confidence": 0.9, "limit": 10})

    with patch("ask_router.call_lmstudio_for_text", return_value=mock_payload):
        result = route_question("unknown question")
        assert result["intent"] == "count_leads"


def test_answer_question_deterministic(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)

    result = answer_question("show top leads", use_llm=False, db_path=db_path)
    assert result["intent"] == "top_leads"
    assert result["data"]["leads"] is not None


def test_answer_question_unknown_fallback(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)

    result = answer_question("what is the meaning of life", use_llm=False, db_path=db_path)
    assert result["intent"] == "search_leads"


def test_answer_question_with_llm_polish(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)

    result = answer_question("show top leads", use_llm=True, db_path=db_path)
    assert result["intent"] == "top_leads"
    assert result["data"]["leads"] is not None
