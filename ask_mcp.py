"""Ask Database — natural language Q&A via LM Studio + read-only Postgres tools (MCP-style)."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Dict, List, Optional

from dotenv import load_dotenv

from llm import chat_completion, is_lm_available

load_dotenv()

MAX_TOOL_ROUNDS = 6

SYSTEM_PROMPT = """You answer questions about leads/contacts stored in PostgreSQL.
Use the provided read-only tools to fetch facts. Never invent data.
If you need data, respond with ONLY a JSON object:
{"tool_call": {"name": "<tool>", "args": {...}}}

Available tools:
- count_companies: args {}
- top_companies: args {"limit": 10}
- companies_without_contacts: args {"limit": 25}
- search_companies: args {"query": "...", "limit": 20}
- search_contacts: args {"query": "...", "limit": 20}
- get_company_summary: args {"name": "..."}

When you have enough data, reply in plain natural language (no JSON).
Keep answers concise. Match the user's language (English or Croatian).
Schema hint: {schema}
/no_think"""


def _try_parse_tool_call(text: str) -> Optional[Dict[str, Any]]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return None
        try:
            parsed = json.loads(m.group())
        except json.JSONDecodeError:
            return None
    if isinstance(parsed, dict) and "tool_call" in parsed:
        return parsed["tool_call"]
    return None


def _serialize(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_serialize(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _build_tool_map() -> Dict[str, Callable[[Dict[str, Any]], Any]]:
    from db_postgres import (
        companies_without_contacts,
        count_companies,
        get_company_summary,
        postgres_schema_hint,
        search_companies,
        search_contacts,
        top_companies,
    )

    def _count(_args: Dict[str, Any]) -> Dict[str, Any]:
        return {"count": count_companies()}

    def _top(args: Dict[str, Any]) -> Dict[str, Any]:
        limit = int(args.get("limit") or 10)
        rows = top_companies(limit=limit)
        return {"companies": _serialize(rows), "count": len(rows)}

    def _no_contacts(args: Dict[str, Any]) -> Dict[str, Any]:
        limit = int(args.get("limit") or 25)
        rows = companies_without_contacts(limit=limit)
        return {"companies": _serialize(rows), "count": len(rows)}

    def _search_co(args: Dict[str, Any]) -> Dict[str, Any]:
        query = str(args.get("query") or "").strip()
        limit = int(args.get("limit") or 20)
        rows = search_companies(query=query, limit=limit)
        return {"companies": _serialize(rows), "count": len(rows)}

    def _search_ct(args: Dict[str, Any]) -> Dict[str, Any]:
        query = str(args.get("query") or "").strip()
        limit = int(args.get("limit") or 20)
        rows = search_contacts(query=query, limit=limit)
        return {"contacts": _serialize(rows), "count": len(rows)}

    def _summary(args: Dict[str, Any]) -> Dict[str, Any]:
        name = str(args.get("name") or "").strip()
        data = get_company_summary(name)
        return _serialize(data) if data else {"error": f"No company matching {name!r}"}

    _ = postgres_schema_hint  # imported for side-effect availability in tests
    return {
        "count_companies": _count,
        "top_companies": _top,
        "companies_without_contacts": _no_contacts,
        "search_companies": _search_co,
        "search_contacts": _search_ct,
        "get_company_summary": _summary,
    }


def _postgres_configured() -> bool:
    return bool(os.getenv("DATABASE_URL"))


def answer_question(question: str, use_llm: bool = True, db_path=None) -> Dict[str, Any]:
    """Answer using LM Studio + read-only Postgres tools. db_path ignored (SQLite ingest unchanged)."""
    _ = db_path
    q = (question or "").strip()
    if not q:
        return {"question": q, "intent": "empty", "answer": "Please enter a question.", "data": None}

    if not _postgres_configured():
        return {
            "question": q,
            "intent": "error",
            "answer": "DATABASE_URL is not set. Copy .env.example to .env and configure PostgreSQL.",
            "data": None,
        }

    if not use_llm:
        try:
            return _answer_without_llm(q)
        except Exception as exc:  # noqa: BLE001
            return {
                "question": q,
                "intent": "error",
                "answer": f"PostgreSQL error: {exc}",
                "data": None,
            }

    if not is_lm_available():
        return {
            "question": q,
            "intent": "error",
            "answer": "LM Studio is not reachable. Start LM Studio and load a model, then try again.",
            "data": None,
        }

    try:
        answer, tool_trace = _run_tool_loop(q)
    except Exception as exc:  # noqa: BLE001
        return {
            "question": q,
            "intent": "error",
            "answer": f"Could not answer from PostgreSQL: {exc}",
            "data": None,
        }

    return {
        "question": q,
        "intent": "postgres_mcp",
        "answer": answer,
        "data": {"tool_calls": tool_trace} if tool_trace else None,
    }


def _answer_without_llm(question: str) -> Dict[str, Any]:
    """Minimal deterministic fallback when LLM checkbox is off."""
    from db_postgres import count_companies, top_companies

    q = question.lower()
    if any(p in q for p in ("how many", "koliko", "count", "broj")):
        n = count_companies()
        return {
            "question": question,
            "intent": "count_companies",
            "answer": f"There are {n} companies in PostgreSQL.",
            "data": {"count": n},
        }
    if "top" in q or "best" in q or "najbolj" in q:
        rows = top_companies(limit=10)
        lines = ["Top companies by fit score:"]
        for row in rows:
            lines.append(f"- {row.get('name')} (fit={row.get('fit_score')}, status={row.get('status')})")
        return {
            "question": question,
            "intent": "top_companies",
            "answer": "\n".join(lines) if rows else "No companies found.",
            "data": {"companies": rows},
        }
    return {
        "question": question,
        "intent": "unknown",
        "answer": "Enable LM Studio for natural-language questions, or ask about counts / top companies.",
        "data": None,
    }


def _run_tool_loop(question: str) -> tuple[str, List[Dict[str, Any]]]:
    from db_postgres import postgres_schema_hint

    tools = _build_tool_map()
    messages: List[Dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT.format(schema=postgres_schema_hint())},
        {"role": "user", "content": question},
    ]
    tool_trace: List[Dict[str, Any]] = []

    for _ in range(MAX_TOOL_ROUNDS):
        raw = chat_completion(messages, temperature=0.0, max_tokens=1500)
        if not raw:
            raise RuntimeError("LM Studio returned empty response")

        tool_call = _try_parse_tool_call(raw)
        if not tool_call:
            return raw.strip(), tool_trace

        name = str(tool_call.get("name") or "").strip()
        args = tool_call.get("args") or {}
        if not isinstance(args, dict):
            args = {}

        fn = tools.get(name)
        if fn is None:
            tool_result = {"error": f"Unknown tool: {name}"}
        else:
            try:
                tool_result = fn(args)
            except Exception as exc:  # noqa: BLE001
                tool_result = {"error": str(exc)}

        tool_trace.append({"tool": name, "args": args, "result": tool_result})
        messages.append({"role": "assistant", "content": json.dumps({"tool_call": {"name": name, "args": args}})})
        messages.append(
            {
                "role": "user",
                "content": f"Tool result for {name}:\n{json.dumps(tool_result, ensure_ascii=False, default=str)}",
            }
        )

    final = chat_completion(
        messages
        + [
            {
                "role": "user",
                "content": "Summarize the tool results above in plain language for the original question.",
            }
        ],
        temperature=0.0,
        max_tokens=1500,
    )
    return (final or "Could not produce an answer.").strip(), tool_trace
