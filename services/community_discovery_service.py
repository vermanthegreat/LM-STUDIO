"""Persistent, incremental topic discovery from operator-provided captures (SQLite).

Discovery only records *which topics exist and what is waiting to be ingested*.
It never fetches anything and never writes conversation content: topics are
ingested separately through ``community_ingest_service.ingest_capture``.

The work queue is derived from stored state (discovered topics that are not yet
ingested, or whose stored post count is below the count last seen), so an
interrupted session resumes by simply reading the queue again.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional

import community_db
import db
from community_discovery import parse_discovery
from community_schemas import DiscoveryResult
from errors import ValidationError

logger = logging.getLogger(__name__)

_STORED = "(SELECT COUNT(*) FROM community_posts p WHERE p.topic_row_id = ct.id)"


def _max_known(*values: Any) -> Any:
    known = [v for v in values if v is not None]
    return max(known) if known else None


def _classify(conn: Any, host: str, topic_id: int, seen_posts: Optional[int], known: bool) -> str:
    row = conn.execute(
        f"SELECT ct.id, {_STORED} AS stored FROM community_topics ct "
        "WHERE ct.forum_host = ? AND ct.topic_id = ?",
        (host, topic_id),
    ).fetchone()
    if row is not None:
        if seen_posts is not None and row["stored"] < seen_posts:
            return "update_available"
        return "ingested"
    return "pending" if known else "new"


def record_discovery(
    store: Any,
    *,
    content: str,
    source_url: str,
    command_log_store: Any = None,
) -> DiscoveryResult:
    """Persist one listing/search capture in a single transaction.

    Identical capture (same source, same content hash) is a no-op. Topics are
    unique per ``(forum_host, topic_id)``; re-sightings update, never duplicate.
    """
    db_path = community_db.require_community_sqlite_runtime(store)
    parsed = parse_discovery(content, source_url=source_url)
    result = DiscoveryResult(
        kind=parsed.kind,
        forum_host=parsed.forum_host,
        source_key=parsed.source_key,
        username=parsed.username,
        replies_in_capture=len(parsed.replies),
        topics_in_capture=len(parsed.topics),
        parse_errors=list(parsed.errors),
        warnings=list(parsed.warnings),
    )
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    now = community_db._now()

    with db.get_conn(db_path) as conn:
        community_db.ensure_community_tables(conn)

    with db.get_conn(db_path) as conn:
        dup = conn.execute(
            "SELECT id FROM community_discovery_runs WHERE forum_host = ? AND source_key = ? AND content_hash = ?",
            (parsed.forum_host, parsed.source_key, content_hash),
        ).fetchone()
        cp = conn.execute(
            "SELECT * FROM community_discovery_checkpoints WHERE forum_host = ? AND source_key = ?",
            (parsed.forum_host, parsed.source_key),
        ).fetchone()
        result.previous_high_water = cp["high_water_last_posted_at"] if cp else None
        if dup is not None:
            result.duplicate_capture = True
            result.run_id = int(dup["id"])
            result.high_water = result.previous_high_water
            return result

        cur = conn.execute(
            """INSERT INTO community_discovery_runs
               (forum_host, kind, source_key, source_url, content_hash, raw_payload,
                topics_in_capture, captured_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (parsed.forum_host, parsed.kind, parsed.source_key, source_url.strip(),
             content_hash, content, len(parsed.topics), now),
        )
        run_id = int(cur.lastrowid)

        for t in parsed.topics:
            existing = conn.execute(
                "SELECT * FROM community_discovered_topics WHERE forum_host = ? AND topic_id = ?",
                (parsed.forum_host, t.topic_id),
            ).fetchone()
            if existing is None:
                cur = conn.execute(
                    """INSERT INTO community_discovered_topics
                       (forum_host, topic_id, slug, title, url, category, posts_count_seen,
                        last_posted_at_seen, first_run_id, last_run_id, first_seen_at, last_seen_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (parsed.forum_host, t.topic_id, t.slug, t.title, t.url, t.category,
                     t.posts_count, t.last_posted_at, run_id, run_id, now, now),
                )
                row_id = int(cur.lastrowid)
            else:
                row_id = int(existing["id"])
                # Counts and dates only move forward, so re-pasting an older page
                # cannot make a topic look less active than it was.
                posts = _max_known(existing["posts_count_seen"], t.posts_count)
                last = _max_known(existing["last_posted_at_seen"], t.last_posted_at)
                conn.execute(
                    """UPDATE community_discovered_topics SET
                           slug = COALESCE(?, slug), title = COALESCE(?, title),
                           category = COALESCE(?, category),
                           posts_count_seen = ?, last_posted_at_seen = ?,
                           last_run_id = ?, times_seen = times_seen + 1, last_seen_at = ?
                       WHERE id = ?""",
                    (t.slug, t.title, t.category, posts, last, run_id, now, row_id),
                )
            kind = _classify(conn, parsed.forum_host, t.topic_id, t.posts_count, existing is not None)
            conn.execute(
                "INSERT INTO community_discovery_sightings VALUES (?, ?, ?, ?, ?)",
                (run_id, row_id, t.posts_count, t.last_posted_at, kind),
            )
            {
                "new": result.new_topics,
                "pending": result.pending_topics,
                "ingested": result.ingested_topics,
                "update_available": result.update_available_topics,
            }[kind].append(t.topic_id)

        if parsed.kind == "user_summary":
            _persist_user_summary(conn, parsed, run_id, now, result)

        dates = [t.last_posted_at for t in parsed.topics if t.last_posted_at]
        high = max(dates + ([result.previous_high_water] if result.previous_high_water else []), default=None)
        prev = result.previous_high_water
        # Pinned topics sit at the top with old dates; they say nothing about paging depth.
        result.reached_checkpoint = bool(
            prev and any(t.last_posted_at and t.last_posted_at <= prev for t in parsed.topics if not t.pinned)
        )
        result.high_water = high
        conn.execute(
            """INSERT INTO community_discovery_checkpoints
               (forum_host, source_key, high_water_last_posted_at, last_run_id, runs_count, updated_at)
               VALUES (?, ?, ?, ?, 1, ?)
               ON CONFLICT(forum_host, source_key) DO UPDATE SET
                   high_water_last_posted_at = excluded.high_water_last_posted_at,
                   last_run_id = excluded.last_run_id,
                   runs_count = runs_count + 1, updated_at = excluded.updated_at""",
            (parsed.forum_host, parsed.source_key, high, run_id, now),
        )
        conn.execute(
            "UPDATE community_discovery_runs SET new_topics = ? WHERE id = ?",
            (len(result.new_topics), run_id),
        )
        result.run_id = run_id

    result.command_id = _log_command(store, command_log_store, result)
    return result


def _persist_user_summary(conn: Any, parsed: Any, run_id: int, now: str, result: DiscoveryResult) -> None:
    norm = parsed.username.strip().lower()
    for r in parsed.replies:
        cur = conn.execute(
            """INSERT OR IGNORE INTO community_user_replies
               (forum_host, username_norm, topic_id, post_number, created_at_src, like_count, first_run_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (parsed.forum_host, norm, r.topic_id, r.post_number, r.created_at, r.like_count, run_id),
        )
        result.new_replies += cur.rowcount
    conn.execute(
        """INSERT INTO community_user_activity
           (forum_host, username_norm, username, stats_json, last_run_id, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(forum_host, username_norm) DO UPDATE SET
               username = excluded.username, stats_json = excluded.stats_json,
               last_run_id = excluded.last_run_id, updated_at = excluded.updated_at""",
        (parsed.forum_host, norm, parsed.username, json.dumps(parsed.stats, sort_keys=True), run_id, now),
    )


def topic_statuses(store: Any) -> list[dict]:
    """Every discovered topic with a deterministic coverage status.

    ``pending``: nothing ingested yet. ``partial``: ingested, but fewer posts are
    stored than the listing/summary reports, or a referenced reply post is not
    stored. ``complete``: no known gap. ``coverage_unknown``: ingested and no gap
    is *known*, but the source gave no post count (absence is not proof).
    """
    db_path = community_db.require_community_sqlite_runtime(store)
    with db.get_conn(db_path) as conn:
        community_db.ensure_community_tables(conn)
        rows = conn.execute(
            f"""SELECT d.*, ct.id AS topic_row_id, {_STORED} AS posts_stored
                FROM community_discovered_topics d
                LEFT JOIN community_topics ct
                  ON ct.forum_host = d.forum_host AND ct.topic_id = d.topic_id
                ORDER BY d.last_posted_at_seen IS NULL, d.last_posted_at_seen DESC, d.id"""
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            refs = [x["post_number"] for x in conn.execute(
                "SELECT DISTINCT post_number FROM community_user_replies WHERE forum_host = ? AND topic_id = ? "
                "ORDER BY post_number", (r["forum_host"], r["topic_id"])).fetchall()]
            stored_numbers = set()
            if r["topic_row_id"] is not None:
                stored_numbers = {x[0] for x in conn.execute(
                    "SELECT post_number FROM community_posts WHERE topic_row_id = ?", (r["topic_row_id"],))}
            item["referenced_reply_posts"] = refs
            item["missing_reply_posts"] = [n for n in refs if n not in stored_numbers]
            seen = r["posts_count_seen"]
            if r["topic_row_id"] is None:
                item["status"] = "pending"
            elif item["missing_reply_posts"] or (seen is not None and r["posts_stored"] < seen):
                item["status"] = "partial"
            elif seen is None:
                item["status"] = "coverage_unknown"
            else:
                item["status"] = "complete"
            out.append(item)
    return out


def work_queue(store: Any, *, limit: int = 200) -> list[dict]:
    """Topics still needing ingestion (``pending`` or ``partial``), deterministic order."""
    return [t for t in topic_statuses(store) if t["status"] in ("pending", "partial")][:limit]


def next_topic(store: Any) -> Optional[dict]:
    """The single next item the operator should capture, or ``None`` when done."""
    queue = work_queue(store, limit=1)
    return queue[0] if queue else None


def list_checkpoints(store: Any) -> list[dict]:
    db_path = community_db.require_community_sqlite_runtime(store)
    with db.get_conn(db_path) as conn:
        community_db.ensure_community_tables(conn)
        return [dict(r) for r in conn.execute(
            "SELECT * FROM community_discovery_checkpoints ORDER BY updated_at DESC"
        ).fetchall()]


def validate_discovery_request(*, raw_text: str, source_url: str, max_chars: int) -> tuple[str, str]:
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
            message="Source URL of the listing or search page is required.",
            status_code=422,
        )
    return text, url


def _log_command(store: Any, command_log_store: Any, result: DiscoveryResult) -> Optional[str]:
    """Counts-only audit entry written after commit; failure never undoes the discovery."""
    try:
        from repositories.command_log_store import get_command_log_store
        from services.command_log import CommandStatus, transition

        log = command_log_store or get_command_log_store(store)
        entry = log.create("Community discovery capture")
        entry.intent = "community_discovery"
        entry.tool_name = "community_discovery"
        entry.risk_class = "local_write"
        entry.tool_arguments = {"forum_host": result.forum_host, "kind": result.kind}
        transition(entry, CommandStatus.PLANNED)
        log.update(entry)
        transition(entry, CommandStatus.EXECUTING)
        log.update(entry)
        transition(entry, CommandStatus.SUCCEEDED)
        entry.result_summary = {
            "run_id": result.run_id,
            "topics_in_capture": result.topics_in_capture,
            "new_topics": len(result.new_topics),
            "update_available": len(result.update_available_topics),
            "replies_in_capture": result.replies_in_capture,
            "new_replies": result.new_replies,
            "parse_errors": len(result.parse_errors),
        }
        log.update(entry)
        return str(entry.id)
    except Exception:  # pragma: no cover
        logger.exception("community discovery command-log write failed")
        return None
