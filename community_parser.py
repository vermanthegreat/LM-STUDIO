"""Deterministic parser for operator-provided Discourse topic captures.

Pure functions: no network, no database, no LLM. Supported inputs:

* ``discourse_json`` - a Discourse topic JSON document (``post_stream.posts``).
* ``rendered_text`` - the LM Studio text capture format documented in
  ``docs/community.md``.

Anything the source does not state stays ``None``. Ambiguous post identities
are rejected into ``errors`` rather than repaired.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

from community_schemas import ParsedPost, ParsedThread, ParsedTopic, PostError
from errors import ValidationError

_TEXT_HEADER_RE = re.compile(
    r"^#(?P<num>\d+)\s+@(?P<user>\S+)\s+(?P<ts>\S+)"
    r"(?:\s+reply-to\s+#(?P<reply>\d+))?"
    r"(?:\s+edited\s+(?P<edited>\S+))?\s*$"
)


def _fail(code: str, message: str) -> ValidationError:
    return ValidationError(error_code=code, message=message, status_code=422)


def parse_topic_url(url: str) -> tuple[str, Optional[str], int, Optional[int]]:
    """Return ``(host, slug, topic_id, post_number)`` for a Discourse topic URL."""
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise _fail("community_invalid_topic_url", "Topic URL must be an http(s) address.")
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2 or parts[0] != "t":
        raise _fail("community_invalid_topic_url", "Topic URL must look like /t/<slug>/<topic_id>.")
    rest = parts[1:]
    slug: Optional[str]
    if len(rest) == 1 and rest[0].isdigit():
        slug, topic_id, post_number = None, int(rest[0]), None
    elif len(rest) == 2 and not rest[0].isdigit() and rest[1].isdigit():
        slug, topic_id, post_number = rest[0], int(rest[1]), None
    elif len(rest) == 3 and not rest[0].isdigit() and rest[1].isdigit() and rest[2].isdigit():
        slug, topic_id, post_number = rest[0], int(rest[1]), int(rest[2])
    else:
        raise _fail("community_invalid_topic_url", "Topic URL has an ambiguous or unsupported path.")
    if topic_id <= 0:
        raise _fail("community_invalid_topic_url", "Topic id must be positive.")
    return parsed.netloc.lower(), slug, topic_id, post_number


def topic_base_url(host: str, slug: Optional[str], topic_id: int, scheme: str = "https") -> str:
    return f"{scheme}://{host}/t/{slug}/{topic_id}" if slug else f"{scheme}://{host}/t/{topic_id}"


def parse_timestamp(value: Any) -> Optional[str]:
    """Normalize an ISO-8601 timestamp to UTC; ``None`` stays ``None``."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return dt.astimezone(timezone.utc).isoformat()


def detect_format(content: str) -> str:
    return "discourse_json" if content.lstrip().startswith("{") else "rendered_text"


def _opt_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("expected integer")
    return value


def _finalize(
    source_format: str,
    topic: ParsedTopic,
    posts: list[ParsedPost],
    errors: list[PostError],
) -> ParsedThread:
    """Reject ambiguous identities: duplicate numbers (unless identical) or duplicate ids."""
    by_number: dict[int, list[ParsedPost]] = {}
    for post in posts:
        by_number.setdefault(post.post_number, []).append(post)
    rejected: set[int] = set()
    kept: list[ParsedPost] = []
    for number, group in by_number.items():
        if len(group) > 1 and any(g != group[0] for g in group[1:]):
            rejected.add(number)
            errors.append(
                PostError(
                    post_number=number,
                    code="duplicate_post_number",
                    message=f"Post number {number} appears more than once with different content.",
                )
            )
        else:
            kept.append(group[0])
    id_counts = Counter(p.post_id for p in kept if p.post_id is not None)
    final: list[ParsedPost] = []
    for post in kept:
        if post.post_id is not None and id_counts[post.post_id] > 1:
            errors.append(
                PostError(
                    post_number=post.post_number,
                    code="duplicate_post_id",
                    message=f"Post id {post.post_id} is attached to more than one post number.",
                )
            )
            continue
        final.append(post)
    final.sort(key=lambda p: p.post_number)
    return ParsedThread(source_format=source_format, topic=topic, posts=final, errors=errors)


def parse_discourse_json(content: str, *, source_url: str) -> ParsedThread:
    host, url_slug, url_topic_id, _ = parse_topic_url(source_url)
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        raise _fail("community_invalid_json", "Capture is not valid JSON.") from None
    if not isinstance(data, dict):
        raise _fail("community_invalid_json", "Capture JSON must be an object.")
    stream = data.get("post_stream")
    raw_posts = stream.get("posts") if isinstance(stream, dict) else None
    if not isinstance(raw_posts, list):
        raise _fail("community_unrecognized_json", "Capture has no post_stream.posts list.")

    json_topic_id = data.get("id")
    if json_topic_id is not None:
        if isinstance(json_topic_id, bool) or not isinstance(json_topic_id, int):
            raise _fail("community_invalid_topic_id", "Topic id in the capture is not an integer.")
        if json_topic_id != url_topic_id:
            raise _fail(
                "community_topic_id_mismatch",
                f"Capture topic id {json_topic_id} does not match the source URL topic id {url_topic_id}.",
            )
    slug = data.get("slug") if isinstance(data.get("slug"), str) and data.get("slug") else url_slug

    errors: list[PostError] = []
    topic_created: Optional[str] = None
    try:
        topic_created = parse_timestamp(data.get("created_at"))
    except ValueError:
        errors.append(PostError(code="invalid_topic_timestamp", message="Topic created_at is not a valid timestamp."))
    declared = data.get("posts_count")
    declared = declared if isinstance(declared, int) and not isinstance(declared, bool) else None
    details = data.get("details") if isinstance(data.get("details"), dict) else {}
    created_by = details.get("created_by") if isinstance(details.get("created_by"), dict) else {}
    category = data.get("category_id")
    topic = ParsedTopic(
        forum_host=host,
        topic_id=url_topic_id,
        topic_url=topic_base_url(host, slug, url_topic_id, urlparse(source_url.strip()).scheme),
        slug=slug,
        title=data.get("title") if isinstance(data.get("title"), str) and data.get("title") else None,
        category=str(category) if category is not None else None,
        author_username=created_by.get("username") if isinstance(created_by.get("username"), str) else None,
        created_at=topic_created,
        posts_count_declared=declared,
    )

    posts: list[ParsedPost] = []
    for index, item in enumerate(raw_posts):
        number_hint: Optional[int] = None
        try:
            if not isinstance(item, dict):
                raise ValueError("post is not an object")
            number = _opt_int(item.get("post_number"))
            number_hint = number
            if number is None or number <= 0:
                raise ValueError("missing or invalid post_number")
            username = item.get("username")
            if not isinstance(username, str) or not username.strip():
                raise ValueError("missing username")
            post_topic_id = _opt_int(item.get("topic_id"))
            if post_topic_id is not None and post_topic_id != url_topic_id:
                raise ValueError(f"post belongs to topic {post_topic_id}, not {url_topic_id}")
            raw_body, cooked = item.get("raw"), item.get("cooked")
            if isinstance(raw_body, str) and raw_body.strip():
                body, body_format = raw_body, "raw"
            elif isinstance(cooked, str) and cooked.strip():
                body, body_format = cooked, "html"
            else:
                raise ValueError("missing body")
            posts.append(
                ParsedPost(
                    post_number=number,
                    username=username.strip(),
                    post_id=_opt_int(item.get("id")),
                    body=body,
                    body_format=body_format,
                    created_at=parse_timestamp(item.get("created_at")),
                    updated_at=parse_timestamp(item.get("updated_at")),
                    reply_to_post_number=_opt_int(item.get("reply_to_post_number")),
                )
            )
        except (ValueError, TypeError) as exc:
            errors.append(
                PostError(
                    post_number=number_hint,
                    code="invalid_post",
                    message=f"Post at index {index}: {exc}",
                )
            )
    return _finalize("discourse_json", topic, posts, errors)


def parse_rendered_text(content: str, *, source_url: str) -> ParsedThread:
    host, url_slug, url_topic_id, _ = parse_topic_url(source_url)
    title: Optional[str] = None
    category: Optional[str] = None
    errors: list[PostError] = []
    posts: list[ParsedPost] = []
    current: Optional[dict[str, Any]] = None
    body_lines: list[str] = []

    def flush() -> None:
        nonlocal current, body_lines
        if current is None:
            return
        body = "\n".join(body_lines).strip("\n").strip()
        if not body:
            errors.append(
                PostError(post_number=current["num"], code="invalid_post", message="Post has an empty body.")
            )
        else:
            try:
                posts.append(
                    ParsedPost(
                        post_number=current["num"],
                        username=current["user"],
                        body=body,
                        body_format="text",
                        created_at=parse_timestamp(current["ts"]),
                        updated_at=parse_timestamp(current["edited"]),
                        reply_to_post_number=current["reply"],
                    )
                )
            except (ValueError, TypeError) as exc:
                errors.append(
                    PostError(post_number=current["num"], code="invalid_post", message=f"Invalid header: {exc}")
                )
        current, body_lines = None, []

    for line in content.splitlines():
        header = _TEXT_HEADER_RE.match(line)
        if header:
            flush()
            current = {
                "num": int(header["num"]),
                "user": header["user"],
                "ts": header["ts"],
                "reply": int(header["reply"]) if header["reply"] else None,
                "edited": header["edited"],
            }
            if current["num"] <= 0:
                errors.append(PostError(post_number=current["num"], code="invalid_post", message="post_number must be positive."))
                current = None
            continue
        if current is not None:
            body_lines.append(line)
            continue
        key, _, value = line.partition(":")
        if key.strip().lower() == "topic" and value.strip() and title is None:
            title = value.strip()
        elif key.strip().lower() == "category" and value.strip() and category is None:
            category = value.strip()
    flush()

    topic = ParsedTopic(
        forum_host=host,
        topic_id=url_topic_id,
        topic_url=topic_base_url(host, url_slug, url_topic_id, urlparse(source_url.strip()).scheme),
        slug=url_slug,
        title=title,
        category=category,
    )
    if not posts and not errors:
        raise _fail("community_no_posts", "No posts were found. Each post must start with '#<n> @<user> <timestamp>'.")
    return _finalize("rendered_text", topic, posts, errors)


def parse_capture(content: str, *, source_url: str) -> ParsedThread:
    if not (content or "").strip():
        raise _fail("community_empty_capture", "Capture is empty.")
    if detect_format(content) == "discourse_json":
        return parse_discourse_json(content, source_url=source_url)
    return parse_rendered_text(content, source_url=source_url)
