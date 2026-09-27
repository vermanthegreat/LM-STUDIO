"""M1 result metadata and deterministic presentation tests."""

from __future__ import annotations

import db
from ask_router import execute_tool_route
from repositories.sqlite_store import SqliteContactStore
from response_presentation import ResponseSpec, validate_response_spec
from services.command_log import InMemoryCommandLog
from services.command_service import CommandService


def _store(tmp_path):
    path = tmp_path / "presentation.db"
    db.init_db(path)
    for name in ("Alpha", "Bravo", "Charlie"):
        db.upsert_lead({"company_name": name}, db_path=path)
    return SqliteContactStore(path)


def test_tool_response_has_unambiguous_metadata_and_preserves_page_order(tmp_path):
    store = _store(tmp_path)
    result = execute_tool_route(
        "find companies",
        "search_contacts",
        {"limit": 2, "offset": 1},
        store=store,
        command_service=CommandService(store, command_log=InMemoryCommandLog()),
    )

    assert result["data"]["record_count"] == 3  # compatibility total
    assert result["data"]["result_metadata"] == {
        "total_matching": 3,
        "returned": 2,
        "requested": 2,
        "offset": 1,
    }
    assert [row["company_name"] for row in result["data"]["records"]] == ["Bravo", "Charlie"]
    assert result["data"]["response_spec"]["mode"] == "table"
    assert "Bravo" in result["answer"]


def test_invalid_response_spec_falls_back_without_accepting_unknown_fields():
    spec, warnings = validate_response_spec(
        "search_contacts",
        {"mode": "table", "title": "Unsafe", "columns": [{"field": "raw_text", "label": "Raw"}]},
    )

    assert spec.title == "Contact search results"
    assert warnings == ["response_spec_invalid"]


def test_valid_response_spec_is_retained():
    spec, warnings = validate_response_spec(
        "search_contacts",
        ResponseSpec(mode="table", title="Companies", columns=[]),
    )

    assert spec.title == "Companies"
    assert warnings == []
