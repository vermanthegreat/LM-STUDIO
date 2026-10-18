"""Tests for local LLM planner integration on /ask."""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import patch
from uuid import UUID

import httpx
import pytest

import db
import llm
from ask_router import answer_question
from config import AppConfig
from repositories.sqlite_store import SqliteContactStore
from services import llm_planner
from services.command_log import CommandStatus, InMemoryCommandLog
from services.command_service import CommandService
from services.llm_planner import PlannerOutcome, run_local_planner
from tools.envelope import ToolResult
from tools.planner import PlannerClarify, PlannerToolCall
from tools.registry import ToolExecutionError, ToolSpec, build_default_registry
from tools.risk import RiskClass
from tools.read_inputs import SearchContactsInput


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


def _outcome(plan=None, error_code=None) -> PlannerOutcome:
    return PlannerOutcome(
        plan=plan,
        error_code=error_code,
        elapsed_ms=12,
        timeout_s=180.0,
        model="local-model",
        reasoning_effort="none",
        finish_reason="stop" if plan else None,
    )


def _setup(tmp_path, registry=None):
    db_path = tmp_path / "ask.db"
    _seed(db_path)
    store = SqliteContactStore(db_path)
    log = InMemoryCommandLog()
    service = CommandService(store, registry=registry, command_log=log)
    return store, service, log


def _ask(question, store, service, outcome):
    with patch.object(llm_planner, "run_local_planner", return_value=outcome) as planner:
        result = answer_question(question, use_llm=True, store=store, command_service=service)
    return result, planner


# ---------------------------------------------------------------- routing


def test_use_llm_false_does_not_call_local_planner(tmp_path):
    store, service, _ = _setup(tmp_path)
    with patch.object(llm_planner, "run_local_planner") as planner:
        result = answer_question("what is the meaning of life", use_llm=False, store=store)

    planner.assert_not_called()
    assert result["intent"] == "search_leads"
    assert "planner" not in result


def test_use_llm_true_does_not_call_planner_for_deterministic_intent(tmp_path):
    store, service, _ = _setup(tmp_path)
    with patch.object(llm_planner, "run_local_planner") as planner:
        result = answer_question("companies without email", use_llm=True, store=store)

    planner.assert_not_called()
    assert result["intent"] == "leads_without_email"
    assert "planner" not in result


def test_valid_plan_executes_once_with_counts_and_terminal_audit(tmp_path):
    store, service, log = _setup(tmp_path)
    plan = PlannerToolCall(
        tool_name="find_companies_missing_email",
        arguments={"missing_definition": "any"},
        reason="User asked about missing email coverage.",
    )
    result, planner = _ask("what is the meaning of life", store, service, _outcome(plan))

    planner.assert_called_once_with("what is the meaning of life")
    assert result["intent"] == "leads_without_email"
    assert result["data"]["tool_name"] == "find_companies_missing_email"
    assert result["data"]["record_count"] == 1
    assert [lead["company_name"] for lead in result["data"]["leads"]] == ["No Email Co"]
    assert result["planner"]["status"] == "ok"
    assert result["planner"]["action"] == "tool"
    entries = list(log._entries.values())
    assert len(entries) == 1
    assert entries[0].status == CommandStatus.SUCCEEDED
    assert entries[0].tool_arguments["missing_definition"] == "any"
    assert str(entries[0].id) == result["data"]["command_id"]


def test_read_command_log_keeps_ids_and_counts_not_private_records(tmp_path):
    store, service, log = _setup(tmp_path)
    plan = PlannerToolCall(tool_name="search_contacts", arguments={"text": "With Email"})
    result, _ = _ask("what is the meaning of life", store, service, _outcome(plan))

    assert result["data"]["record_count"] == 1
    entry = log.get(UUID(result["data"]["command_id"]))
    summary = entry.result_summary
    blob = json.dumps(summary)
    assert "hello@example.com" not in blob
    assert "With Email Co" not in blob
    assert summary["record_count"] == 1
    assert summary["returned_count"] == 1
    assert summary["records"] == [{"id": result["data"]["records"][0]["id"]}]
    assert result["data"]["records"][0]["company_name"] == "With Email Co"


@pytest.mark.parametrize(
    "plan",
    [
        PlannerToolCall(tool_name="drop_all_tables", arguments={}),
        PlannerToolCall(tool_name="search_contacts", arguments={"sql": "DELETE FROM leads"}),
        PlannerToolCall(tool_name="search_contacts", arguments={"limit": 100000}),
    ],
)
def test_invalid_plans_execute_nothing_and_are_rejected(tmp_path, plan):
    store, service, log = _setup(tmp_path)
    with patch("tools.read_handlers.handle_search_contacts") as handler:
        result, _ = _ask("anything unusual", store, service, _outcome(plan))

    handler.assert_not_called()
    assert result["intent"] == "tool_error"
    assert result["data"]["error_code"] in {"UnknownToolError", "ToolValidationError"}
    entry = log.get(UUID(result["data"]["command_id"]))
    assert entry.status == CommandStatus.REJECTED
    assert len(store.list_leads()) == 2


def test_propose_tool_selected_by_planner_only_previews(tmp_path):
    store, service, log = _setup(tmp_path)
    lead_id = store.list_leads()[0]["id"]
    plan = PlannerToolCall(
        tool_name="propose_create_followup",
        arguments={"lead_id": lead_id, "title": "Call back", "due_date": "2026-11-02"},
    )
    with db.get_conn(tmp_path / "ask.db") as conn:
        before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    result, _ = _ask("please remind me later", store, service, _outcome(plan))
    with db.get_conn(tmp_path / "ask.db") as conn:
        after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

    assert after == before
    entry = log.get(UUID(result["data"]["command_id"]))
    assert entry.status == CommandStatus.AWAITING_APPROVAL


def _error_registry(handler):
    registry = build_default_registry()
    registry._tools["search_contacts"] = ToolSpec(
        name="search_contacts",
        risk_class=RiskClass.READ,
        input_model=SearchContactsInput,
        handler=handler,
        description="patched",
    )
    return registry


def test_handler_error_envelope_is_reported_as_failure(tmp_path):
    def handler(store, args):
        return ToolResult(
            tool_name="search_contacts",
            status="error",
            summary="Runtime unsupported.",
            warnings=["runtime_unsupported"],
        )

    store, service, log = _setup(tmp_path, registry=_error_registry(handler))
    result, _ = _ask("odd question", store, service, _outcome(PlannerToolCall(tool_name="search_contacts")))

    assert result["intent"] == "tool_error"
    assert result["data"]["error_code"] == "runtime_unsupported"
    assert result["data"]["command_status"] == "failed"
    entry = log.get(UUID(result["data"]["command_id"]))
    assert entry.status == CommandStatus.FAILED
    assert entry.error_code == "runtime_unsupported"


def test_handler_exception_leaves_failed_terminal_state(tmp_path):
    def handler(store, args):
        raise RuntimeError("secret row content hello@example.com")

    store, service, log = _setup(tmp_path, registry=_error_registry(handler))
    result, _ = _ask("odd question", store, service, _outcome(PlannerToolCall(tool_name="search_contacts")))

    assert result["intent"] == "tool_error"
    assert result["data"]["error_code"] == "ToolExecutionError"
    entry = log.get(UUID(result["data"]["command_id"]))
    assert entry.status == CommandStatus.FAILED
    assert entry.error_code == "tool_execution_error"
    assert "hello@example.com" not in (entry.error_message or "")


def test_unexpected_exception_raises_typed_error_from_service(tmp_path):
    def handler(store, args):
        raise KeyError("boom")

    store, service, log = _setup(tmp_path, registry=_error_registry(handler))
    entry = service.receive("q")
    service.plan_tool_call(entry, PlannerToolCall(tool_name="search_contacts"))
    with pytest.raises(ToolExecutionError):
        service.execute_planned_tool(entry)
    assert log.get(entry.id).status == CommandStatus.FAILED


def test_clarification_is_audited(tmp_path):
    store, service, log = _setup(tmp_path)
    result, _ = _ask("where", store, service, _outcome(PlannerClarify(question="Which city?")))

    assert result["intent"] == "clarify"
    assert result["answer"] == "Which city?"
    entry = log.get(UUID(result["data"]["command_id"]))
    assert entry.status == CommandStatus.PLANNED
    assert entry.intent == "clarify"
    assert entry.result_summary == {"question": "Which city?"}
    assert entry.tool_name is None


def test_planner_failure_falls_back_with_disclosed_metadata(tmp_path):
    store, service, log = _setup(tmp_path)
    with patch("ask_router.call_lmstudio_for_text", return_value=None):
        result, _ = _ask("what is the meaning of life", store, service, _outcome(None, "planner_timeout"))

    assert result["intent"] == "search_leads"
    assert result["answer"]
    assert result["planner"]["status"] == "failed"
    assert result["planner"]["error_code"] == "planner_timeout"
    assert result["planner"]["fallback"] == "deterministic"
    assert result["planner"]["timeout_s"] == 180.0


# ------------------------------------------------------- LM Studio adapter


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300

    def json(self):
        return self._payload


class _FakeClient:
    captured: list[dict] = []
    timeouts: list[float] = []
    reply: object = None

    def __init__(self, timeout):
        type(self).timeouts.append(timeout)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def post(self, url, json):
        type(self).captured.append(json)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _message(content, *, reasoning=None, finish="stop", status=200):
    message = {"content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return _FakeResponse({"choices": [{"message": message, "finish_reason": finish}]}, status)


_CONFIG = replace(AppConfig(), planner_timeout=42.0, planner_max_tokens=300, planner_reasoning_effort="none")


@pytest.fixture
def fake_lm(monkeypatch):
    _FakeClient.captured = []
    _FakeClient.timeouts = []
    monkeypatch.setattr(llm.httpx, "Client", _FakeClient)
    return _FakeClient


def _run(fake, reply, config=_CONFIG, question="Which partner agencies operate in Berlin?"):
    fake.reply = reply
    return run_local_planner(question, config=config)


def test_planner_request_uses_bounded_planner_options(fake_lm):
    outcome = _run(fake_lm, _message('{"action":"clarify","question":"Which city?"}'))

    assert isinstance(outcome.plan, PlannerClarify)
    sent = fake_lm.captured[0]
    assert sent["reasoning_effort"] == "none"
    assert sent["max_tokens"] == 300
    assert fake_lm.timeouts == [42.0]
    assert outcome.metadata()["timeout_s"] == 42.0


def test_reasoning_effort_can_be_omitted(fake_lm):
    config = replace(_CONFIG, planner_reasoning_effort=None)
    _run(fake_lm, _message('{"action":"clarify","question":"?"}'), config=config)
    assert "reasoning_effort" not in fake_lm.captured[0]


def test_planner_prompt_lists_argument_schemas(fake_lm):
    _run(fake_lm, _message('{"action":"clarify","question":"?"}'))
    prompt = fake_lm.captured[0]["messages"][1]["content"]
    assert "find_companies_missing_email [read]" in prompt
    assert "missing_definition?: one of any|non_rejected|verified" in prompt
    assert "limit?: integer [>=1, <=100] = 25" in prompt
    assert "propose_create_followup [propose]" in prompt


def test_reasoning_channel_is_never_parsed(fake_lm):
    tool_json = '{"action":"tool","tool_name":"search_contacts","arguments":{}}'
    outcome = _run(fake_lm, _message("", reasoning=tool_json))
    assert outcome.plan is None
    assert outcome.error_code == "planner_reasoning_only"

    outcome = _run(fake_lm, _message("thinking out loud", reasoning=tool_json))
    assert outcome.plan is None
    assert outcome.error_code == "planner_malformed_json"


@pytest.mark.parametrize(
    ("reply", "code"),
    [
        (_message(""), "planner_empty_output"),
        (_message(None), "planner_empty_output"),
        (_message('{"action":"tool","tool_name":"sea', finish="length"), "planner_truncated"),
        (_message("", finish="length"), "planner_truncated"),
        (_message("<think>hmm</think>{\"action\":\"clarify\",\"question\":\"?\"}"), "planner_reasoning_in_content"),
        (_message("[1, 2]"), "planner_malformed_json"),
        (_message('{"action":"tool"}'), "planner_invalid_plan"),
        (_message('{"action":"tool","tool_name":"x","extra":1}'), "planner_invalid_plan"),
        (_message('{"action":"run_sql","sql":"DROP TABLE leads"}'), "planner_invalid_plan"),
        (_message("ok", status=500), "planner_http_error"),
        (_FakeResponse({"unexpected": True}), "planner_malformed_response"),
        (httpx.ReadTimeout("slow"), "planner_timeout"),
        (httpx.ConnectError("refused"), "planner_unavailable"),
    ],
)
def test_planner_failures_are_attributed(fake_lm, reply, code):
    outcome = _run(fake_lm, reply)
    assert outcome.plan is None
    assert outcome.error_code == code
    meta = outcome.metadata()
    assert meta["status"] == "failed"
    assert meta["stage"] == "planning"
    assert meta["error_code"] == code


def test_fenced_final_json_is_accepted(fake_lm):
    outcome = _run(fake_lm, _message('```json\n{"action":"tool","tool_name":"search_contacts","arguments":{"text":"x"}}\n```'))
    assert isinstance(outcome.plan, PlannerToolCall)
    assert outcome.plan.arguments == {"text": "x"}


def test_empty_question_makes_no_request(fake_lm):
    outcome = _run(fake_lm, _message("{}"), question="   ")
    assert outcome.error_code == "planner_empty_question"
    assert fake_lm.captured == []


def test_planner_config_from_env(monkeypatch):
    monkeypatch.setenv("LMSTUDIO_PLANNER_TIMEOUT", "75")
    monkeypatch.setenv("LMSTUDIO_PLANNER_MAX_TOKENS", "256")
    monkeypatch.setenv("LMSTUDIO_PLANNER_REASONING_EFFORT", "omit")
    cfg = AppConfig.from_env()
    assert (cfg.planner_timeout, cfg.planner_max_tokens, cfg.planner_reasoning_effort) == (75.0, 256, None)
    monkeypatch.delenv("LMSTUDIO_PLANNER_REASONING_EFFORT")
    assert AppConfig.from_env().planner_reasoning_effort == "none"


def test_other_lm_studio_calls_keep_their_own_timeout(fake_lm):
    fake_lm.reply = _message('{"intent":"count_leads"}')
    assert llm.call_lmstudio_for_text("classify this") == '{"intent": "count_leads"}'
    assert fake_lm.timeouts == [8.0]
    assert "reasoning_effort" not in fake_lm.captured[0]


# ------------------------------------------------------------------ HTTP


def test_ask_routes_disclose_planner_outcome(tmp_path):
    from fastapi.testclient import TestClient

    from app import create_app

    cfg = AppConfig(database_path=tmp_path / "routes.db", port=8025)
    with TestClient(create_app(cfg), base_url="http://127.0.0.1:8025") as client:
        with patch.object(llm_planner, "run_local_planner", return_value=_outcome(None, "planner_timeout")):
            with patch("ask_router.call_lmstudio_for_text", return_value=None):
                as_json = client.post(
                    "/ask", json={"question": "what is the meaning of life", "use_llm": True}
                ).json()
                page = client.post(
                    "/ask", data={"question": "what is the meaning of life", "use_llm": "true"}
                ).text

    assert as_json["planner"]["error_code"] == "planner_timeout"
    assert as_json["planner"]["fallback"] == "deterministic"
    assert "Local planner: failed (planner_timeout)" in page
    assert "deterministic fallback" in page
