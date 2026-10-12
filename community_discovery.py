"""Deterministic parser for operator-provided Discourse listing/search captures.

Pure functions: no network, no database, no LLM. Supported inputs (JSON only):

* ``listing`` - ``{"topic_list": {"topics": [...]}}`` (``/latest.json``, ``/c/.../l/latest.json``).
* ``search``  - ``{"topics": [...], "posts": [...]}`` (``/search.json?q=...``).

Both shapes follow the documented Discourse API; compatibility with live Shopify
Community output is UNVERIFIED (see docs/community.md).
"""

from __future__ import annotations

import json
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlparse

from community_parser import parse_timestamp, topic_base_url
from community_schemas import DiscoveredTopic, ParsedDiscovery, PostError
from errors import ValidationError


def _fail(code: str, message: str) -> ValidationError:
    return ValidationError(error_code=code, message=message, status_code=422)


def normalize_source(url: str) -> tuple[str, str, str]:
    """Return ``(scheme, host, source_key)``; page/fragment are not part of the key.

    Paging a listing (``?page=2``) must keep one checkpoint per listing.
    """
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise _fail("community_invalid_source_url", "Source URL must be an http(s) address.")
    path = parsed.path.rstrip("/") or "/"
    if path.endswith(".json"):
        path = path[: -len(".json")] or "/"
    query = sorted((k, v) for k, v in parse_qsl(parsed.query) if k.lower() != "page")
    key = path + (("?" + urlencode(query)) if query else "")
    return parsed.scheme, parsed.netloc.lower(), key


def _opt_int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def parse_discovery(content: str, *, source_url: str) -> ParsedDiscovery:
    scheme, host, key = normalize_source(source_url)
    try:
        data = json.loads(content)
    except (TypeError, ValueError):
        raise _fail("community_discovery_invalid_json", "Discovery capture must be Discourse JSON.") from None
    if not isinstance(data, dict):
        raise _fail("community_discovery_invalid_json", "Discovery capture must be a JSON object.")

    if isinstance(data.get("topic_list"), dict) and isinstance(data["topic_list"].get("topics"), list):
        kind, rows = "listing", data["topic_list"]["topics"]
    elif isinstance(data.get("topics"), list) and ("posts" in data or "grouped_search_result" in data):
        kind, rows = "search", data["topics"]
    else:
        raise _fail(
            "community_discovery_unrecognized",
            "Capture is neither a topic listing (topic_list.topics) nor a search result (topics + posts).",
        )
    prefix = "search:" if kind == "search" else "listing:"
    out = ParsedDiscovery(kind=kind, forum_host=host, source_key=prefix + key)

    seen: set[int] = set()
    for index, row in enumerate(rows):
        tid = row.get("id") if isinstance(row, dict) else None
        if not isinstance(tid, int) or isinstance(tid, bool) or tid <= 0:
            out.errors.append(
                PostError(code="invalid_topic_id", message=f"Row {index} has no positive integer topic id.")
            )
            continue
        if tid in seen:
            out.warnings.append(f"Topic {tid} appears more than once in the capture; counted once.")
            continue
        try:
            last = parse_timestamp(row.get("last_posted_at"))
        except (ValueError, TypeError):
            last = None
            out.warnings.append(f"Topic {tid}: unparseable last_posted_at ignored.")
        seen.add(tid)
        slug = row.get("slug") if isinstance(row.get("slug"), str) and row.get("slug") else None
        cat = row.get("category_id")
        out.topics.append(
            DiscoveredTopic(
                topic_id=tid,
                slug=slug,
                title=row.get("title") if isinstance(row.get("title"), str) else None,
                category=str(cat) if isinstance(cat, int) and not isinstance(cat, bool) else None,
                posts_count=_opt_int(row.get("posts_count")),
                last_posted_at=last,
                pinned=bool(row.get("pinned")),
                url=topic_base_url(host, slug, tid, scheme),
            )
        )
    return out
