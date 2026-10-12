"""Deterministic parser for operator-provided Discourse listing/search captures.

Pure functions: no network, no database, no LLM. Supported inputs (JSON only):

* ``listing`` - ``{"topic_list": {"topics": [...]}}`` (``/latest.json``, ``/c/.../l/latest.json``).
* ``search``  - ``{"topics": [...], "posts": [...]}`` (``/search.json?q=...``).
* ``user_summary`` - ``{"user_summary": {...}, "topics": [...], "users": [...]}``
  (``/u/<name>/summary.json``): topic ids, titles and the user's own reply references.

Both shapes follow the documented Discourse API; compatibility with live Shopify
Community output is UNVERIFIED (see docs/community.md).
"""

from __future__ import annotations

import json
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlparse

from community_parser import parse_timestamp, topic_base_url
from community_schemas import DiscoveredTopic, ParsedDiscovery, PostError, UserReplyRef
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

    if isinstance(data.get("user_summary"), dict):
        return _parse_user_summary(data, scheme, host, key, source_url)
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


_STAT_KEYS = (
    "likes_given", "likes_received", "topics_entered", "posts_read_count", "days_visited",
    "topic_count", "post_count", "time_read", "recent_time_read", "bookmark_count",
)


def _summary_username(source_url: str) -> Optional[str]:
    parts = [p for p in urlparse(source_url.strip()).path.split("/") if p]
    if len(parts) >= 3 and parts[0] in ("u", "users") and parts[2].removesuffix(".json") == "summary":
        return parts[1]
    return None


def _parse_user_summary(data: dict, scheme: str, host: str, key: str, source_url: str) -> ParsedDiscovery:
    summary = data["user_summary"]
    username = _summary_username(source_url)
    if username is None:
        raise _fail(
            "community_user_summary_username_unknown",
            "Source URL must look like https://<host>/u/<username>/summary so the user is identified.",
        )
    users = data.get("users") if isinstance(data.get("users"), list) else []
    listed = {str(u.get("username", "")).lower() for u in users if isinstance(u, dict)}
    out = ParsedDiscovery(kind="user_summary", forum_host=host, source_key="user_summary:" + key, username=username)
    if listed and username.lower() not in listed:
        out.warnings.append("Username from the source URL is not among the capture's users[]; identity unconfirmed.")
    out.stats = {
        k: summary[k] for k in _STAT_KEYS
        if isinstance(summary.get(k), int) and not isinstance(summary.get(k), bool)
    }

    meta: dict[int, dict] = {}
    for row in data.get("topics") if isinstance(data.get("topics"), list) else []:
        if isinstance(row, dict) and isinstance(row.get("id"), int) and not isinstance(row["id"], bool) and row["id"] > 0:
            meta[row["id"]] = row

    def valid_id(v: Any) -> bool:
        return isinstance(v, int) and not isinstance(v, bool) and v > 0

    ids: set[int] = set(meta)
    ids.update(v for v in summary.get("topic_ids", []) if valid_id(v)) if isinstance(summary.get("topic_ids"), list) else None
    seen_replies: set[tuple[int, int]] = set()
    for index, rep in enumerate(summary.get("replies") if isinstance(summary.get("replies"), list) else []):
        tid = rep.get("topic_id") if isinstance(rep, dict) else None
        num = rep.get("post_number") if isinstance(rep, dict) else None
        if not valid_id(tid) or not valid_id(num):
            out.errors.append(PostError(code="invalid_reply_reference",
                                        message=f"Reply row {index} lacks a positive topic_id and post_number."))
            continue
        if (tid, num) in seen_replies:
            out.warnings.append(f"Reply {tid}#{num} appears more than once; counted once.")
            continue
        seen_replies.add((tid, num))
        ids.add(tid)
        try:
            created = parse_timestamp(rep.get("created_at"))
        except (ValueError, TypeError):
            created = None
            out.warnings.append(f"Reply {tid}#{num}: unparseable created_at ignored.")
        likes = rep.get("like_count")
        out.replies.append(UserReplyRef(
            topic_id=tid, post_number=num, created_at=created,
            like_count=likes if isinstance(likes, int) and not isinstance(likes, bool) else None))
    out.replies.sort(key=lambda r: (r.topic_id, r.post_number))

    for tid in sorted(ids):  # deterministic order
        row = meta.get(tid, {})
        slug = row.get("slug") if isinstance(row.get("slug"), str) and row.get("slug") else None
        try:
            last = parse_timestamp(row.get("last_posted_at"))
        except (ValueError, TypeError):
            last = None
        cat = row.get("category_id")
        title = row.get("title") if isinstance(row.get("title"), str) else None
        out.topics.append(DiscoveredTopic(
            topic_id=tid, slug=slug, title=title,
            category=str(cat) if isinstance(cat, int) and not isinstance(cat, bool) else None,
            posts_count=_opt_int(row.get("posts_count")), last_posted_at=last,
            pinned=False, url=topic_base_url(host, slug, tid, scheme)))
    return out
