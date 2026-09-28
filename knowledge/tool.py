"""Typed read tool: ``search_knowledge``."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from knowledge.ingestion import KnowledgeRuntimeUnsupportedError, require_knowledge_sqlite_runtime
from knowledge.retrieval import search_knowledge
from knowledge.schemas import SearchKnowledgeInput
from tools.envelope import ToolResult

_RECORD_FIELDS = (
    "id", "original_filename", "mime_type", "content_kind", "captured_at", "event_date",
    "event_date_source", "project", "category", "sub_category", "summary", "topics",
    "snippet", "ranking", "source", "status",
)


def handle_search_knowledge(store: Any, args: BaseModel) -> ToolResult:
    params = SearchKnowledgeInput.model_validate(args)
    try:
        database_path = require_knowledge_sqlite_runtime(store)
    except KnowledgeRuntimeUnsupportedError as exc:
        return ToolResult(
            tool_name="search_knowledge",
            status="error",
            summary=exc.message,
            warnings=[exc.error_code],
            provenance=["knowledge_runtime:unsupported"],
        )
    result = search_knowledge(database_path, params)
    records: list[dict[str, Any]] = []
    for hit in result["hits"]:
        record = {key: hit.get(key) for key in _RECORD_FIELDS}
        record["entities"] = [e["name"] for e in hit.get("entities", [])]
        records.append(record)
    summary = f"Found {result['count']} knowledge item(s) ({result['engine']}, {result['match_mode']})."
    return ToolResult(
        tool_name="search_knowledge",
        status="ok",
        summary=summary,
        records=records,
        record_count=result["count"],
        provenance=["repository:knowledge_search"] + [f"knowledge_item:{r['id']}" for r in records],
        warnings=[] if result["count"] else ["no_matches_absence_is_not_evidence"],
    )
