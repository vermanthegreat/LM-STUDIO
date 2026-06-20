"""Tests for local LLM planner integration on /ask."""

from __future__ import annotations

from unittest.mock import patch

import db
from ask_router import answer_question
from repositories.sqlite_store import SqliteContactStore
from services.command_log import InMemoryCommandLog
from services.command_service import CommandService
from tools.planner import PlannerToolCall


def _seed(db_path):
    db.init_db(db_path)
    db.upsert_lead(
        {
            "company_name": "With Email Co",
            "company_email": "hello@example.com",
            "fit_score": 80,
        },
        db_path=db_path,
    )
    db.upsert_lead({"company_name": "No Email Co", "fit_score": 40}, db_path=db_path)


def test_use_llm_false_does_not_call_local_planner(tmp_path):
    db_path = tmp_path / "ask.db"
    _seed(db_path)
    store = SqliteContactStore(db_path)

    with patch("services.llm_planner.plan_question_with_local_llm") as planner:
        result = answer_question(
            "what is the meaning of life",
            use_llm=False,
            store=store,
        )

    planner.assert_not_called()
    assert result["intent"] == "search_leads"


def test_use_llm_true_calls_local_planner_for_unknown_question(tmp_path):
    db_path = tmp_path / "ask.db"
    _seed(db_path)
    store = SqliteContactStore(db_path)
    service = CommandService(store, command_log=InMemoryCommandLog())
    planner_payload = {
        "action": "tool",
        "tool_name": "find_companies_missing_email",
        "arguments": {"missing_definition": "any"},
        "reason": "User asked about missing email coverage.",
    }

    with patch(
        "services.llm_planner.plan_question_with_local_llm",
        return_value=PlannerToolCall.model_validate(planner_payload),
    ) as planner:
        result = answer_question(
            "what is the meaning of life",
            use_llm=True,
            store=store,
            command_service=service,
        )

    planner.assert_called_once_with("what is the meaning of life")
    assert result["intent"] == "leads_without_email"
    assert result["data"]["tool_name"] == "find_companies_missing_email"
    assert result["data"]["record_count"] == 1


def test_llm_planner_unavailable_falls_back_without_crashing(tmp_path):
    db_path = tmp_path / "ask.db"
    _seed(db_path)
    store = SqliteContactStore(db_path)

    with patch("services.llm_planner.plan_question_with_local_llm", return_value=None):
        with patch("ask_router.call_lmstudio_for_text", return_value=None):
            result = answer_question(
                "what is the meaning of life",
                use_llm=True,
                store=store,
            )

    assert result["intent"] == "search_leads"
    assert result["answer"]
    assert "question" in result


def test_use_llm_true_does_not_call_planner_for_deterministic_intent(tmp_path):
    db_path = tmp_path / "ask.db"
    _seed(db_path)
    store = SqliteContactStore(db_path)

    with patch("services.llm_planner.plan_question_with_local_llm") as planner:
        result = answer_question(
            "companies without email",
            use_llm=True,
            store=store,
        )

    planner.assert_not_called()
    assert result["intent"] == "leads_without_email"
