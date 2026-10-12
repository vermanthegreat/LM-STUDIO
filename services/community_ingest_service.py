"""Idempotent ingestion of operator-provided Discourse topic captures (SQLite)."""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Optional
from urllib.parse import urlparse

import community_db
import db
from community_parser import parse_capture, parse_topic_url
from community_schemas import IngestResult, ParsedPost, PostError
from errors import ValidationError
from providers.gmail_normalize import html_to_text

logger = logging.getLogger(__name__)

_WS_RE = re.compile(r"\s+")


def normalized_body(body: str, body_format: str) -> str:
    text = html_to_text(body) if body_format == "html" else body
    return _WS_RE.sub(" ", text).strip()


def body_hash(body: str, body_format: str) -> str:
    return hashlib.sha256(normalized_body(body, body_format).encode("utf-8")).hexdigest()


def _post_url(topic_url: str, post_number: int) -> str:
    return f"{topic_url}/{post_number}"


def _apply_post(
    conn: Any,
    *,
    topic_row_id: int,
    host: str,
    topic_url: str,
    post: ParsedPost,
    capture_id: int,
    result: IngestResult,
) -> None:
    digest = body_hash(post.body, post.body_format)
    existing = community_db.get_post(conn, topic_row_id, post.post_number)

    if post.post_id is not None:
        owner = community_db.post_id_owner(conn, topic_row_id, post.post_id)
        if owner is not None and owner != post.post_number:
            result.parse_errors.append(
                PostError(
                    post_number=post.post_number,
                    code="post_id_conflict",
                    message=f"Post id {post.post_id} is already stored for post #{owner}.",
                )
            )
            return

    if existing is None:
        author_id = community_db.get_or_create_author(conn, host, post.username)
        community_db.insert_post(
            conn,
            topic_row_id=topic_row_id,
            author_id=author_id,
            post=post,
            body_hash=digest,
            post_url=_post_url(topic_url, post.post_number),
            capture_id=capture_id,
        )
        result.new_posts += 1
        result.new_post_numbers.append(post.post_number)
        return

    if existing["author_norm"] != post.username.strip().lower():
        result.parse_errors.append(
            PostError(
                post_number=post.post_number,
                code="author_conflict",
                message="Stored author differs from the captured author; post left unchanged.",
            )
        )
        return
    if (
        existing["post_id"] is not None
        and post.post_id is not None
        and existing["post_id"] != post.post_id
    ):
        result.parse_errors.append(
            PostError(
                post_number=post.post_number,
                code="post_id_conflict",
                message="Stored post id differs from the captured post id; post left unchanged.",
            )
        )
        return

    if digest == existing["body_hash"]:
        community_db.touch_post(conn, existing, post, capture_id)
        result.unchanged_posts += 1
        return

    if existing["body_format"] != post.body_format:
        # Different representations (e.g. raw markdown vs cooked HTML) are not comparable.
        result.warnings.append(
            f"Post #{post.post_number}: body format changed "
            f"({existing['body_format']} -> {post.body_format}); content not compared."
        )
        community_db.touch_post(conn, existing, post, capture_id)
        result.unchanged_posts += 1
        return

    community_db.revise_post(conn, existing, post, digest, capture_id)
    result.edited_posts += 1
    result.edited_post_numbers.append(post.post_number)


def ingest_capture(
    store: Any,
    *,
    content: str,
    source_url: str,
    command_log_store: Any = None,
) -> IngestResult:
    """Parse and persist one capture in a single transaction.

    Any exception rolls the whole capture back. Posts absent from a capture are
    never deleted. An identical capture (same topic, same content hash) is a no-op.
    """
    db_path = community_db.require_community_sqlite_runtime(store)
    thread = parse_capture(content, source_url=source_url)
    topic = thread.topic
    result = IngestResult(
        forum_host=topic.forum_host,
        topic_id=topic.topic_id,
        source_format=thread.source_format,
        posts_processed=len(thread.posts),
        parse_errors=list(thread.errors),
    )
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    with db.get_conn(db_path) as conn:
        community_db.ensure_community_tables(conn)

    with db.get_conn(db_path) as conn:
        duplicate = community_db.find_capture(conn, topic.forum_host, topic.topic_id, content_hash)
        if duplicate is not None:
            row = conn.execute(
                "SELECT id FROM community_topics WHERE forum_host = ? AND topic_id = ?",
                (topic.forum_host, topic.topic_id),
            ).fetchone()
            result.duplicate_capture = True
            result.capture_id = int(duplicate["id"])
            result.topic_row_id = int(row["id"]) if row else None
            result.unchanged_posts = len(thread.posts)
            return result

        topic_row_id = community_db.upsert_topic(conn, topic)
        capture_id = community_db.insert_capture(
            conn,
            host=topic.forum_host,
            topic_id=topic.topic_id,
            source_url=source_url.strip(),
            source_format=thread.source_format,
            content_hash=content_hash,
            raw_payload=content,
        )
        for post in thread.posts:
            _apply_post(
                conn,
                topic_row_id=topic_row_id,
                host=topic.forum_host,
                topic_url=topic.topic_url,
                post=post,
                capture_id=capture_id,
                result=result,
            )
        stored = conn.execute(
            "SELECT COUNT(*) FROM community_posts WHERE topic_row_id = ?", (topic_row_id,)
        ).fetchone()[0]
        if topic.posts_count_declared is not None and stored < topic.posts_count_declared:
            result.incomplete_capture = True
            result.warnings.append(
                f"Stored {stored} of {topic.posts_count_declared} declared posts; capture is incomplete."
            )
        community_db.finish_capture(
            conn,
            capture_id,
            posts=result.posts_processed,
            new=result.new_posts,
            edited=result.edited_posts,
            unchanged=result.unchanged_posts,
            errors=len(result.parse_errors),
        )
        result.topic_row_id = topic_row_id
        result.capture_id = capture_id

    result.command_id = _log_command(store, command_log_store, result)
    return result


def _log_command(store: Any, command_log_store: Any, result: IngestResult) -> Optional[str]:
    """Counts-only audit entry written after commit; failure never undoes the ingest."""
    try:
        from repositories.command_log_store import get_command_log_store
        from services.command_log import CommandStatus, transition

        log = command_log_store or get_command_log_store(store)
        entry = log.create("Community capture ingest")
        entry.intent = "community_ingest"
        entry.tool_name = "community_ingest"
        entry.risk_class = "local_write"
        entry.tool_arguments = {"forum_host": result.forum_host, "topic_id": result.topic_id}
        transition(entry, CommandStatus.PLANNED)
        log.update(entry)
        transition(entry, CommandStatus.EXECUTING)
        log.update(entry)
        transition(entry, CommandStatus.SUCCEEDED)
        entry.result_summary = {
            "posts_processed": result.posts_processed,
            "new_posts": result.new_posts,
            "edited_posts": result.edited_posts,
            "unchanged_posts": result.unchanged_posts,
            "parse_errors": len(result.parse_errors),
        }
        log.update(entry)
        return str(entry.id)
    except Exception:  # pragma: no cover - audit failure must not mask a committed ingest
        logger.exception("community ingest command-log write failed")
        return None


def validate_ingest_request(*, raw_text: str, source_url: str, max_chars: int) -> tuple[str, str]:
    text = raw_text or ""
    if not text.strip():
        raise ValidationError(error_code="empty_raw_text", message="Capture cannot be empty.", status_code=422)
    if len(text) > max_chars:
        raise ValidationError(
            error_code="raw_text_too_long",
            message=f"Capture exceeds the maximum of {max_chars} characters.",
            status_code=422,
        )
    url = (source_url or "").strip()
    if not url:
        raise ValidationError(
            error_code="community_source_url_required",
            message="Topic URL is required (e.g. https://host/t/slug/123).",
            status_code=422,
        )
    parse_topic_url(url)
    if urlparse(url).scheme not in ("http", "https"):  # defensive; parse_topic_url enforces it
        raise ValidationError(error_code="invalid_source_url", message="Invalid URL.", status_code=422)
    return text, url
