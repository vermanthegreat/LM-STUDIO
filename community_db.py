"""SQLite persistence for Shopify Community (Discourse) conversation captures.

Additive tables only. Nothing here touches leads, people, interactions or tasks.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

COMMUNITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS community_captures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    forum_host TEXT NOT NULL,
    topic_id INTEGER NOT NULL,
    source_url TEXT NOT NULL,
    source_format TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    raw_payload TEXT NOT NULL,
    posts_in_capture INTEGER NOT NULL DEFAULT 0,
    new_posts INTEGER NOT NULL DEFAULT 0,
    edited_posts INTEGER NOT NULL DEFAULT 0,
    unchanged_posts INTEGER NOT NULL DEFAULT 0,
    parse_error_count INTEGER NOT NULL DEFAULT 0,
    captured_at TEXT NOT NULL,
    UNIQUE(forum_host, topic_id, content_hash)
);

CREATE TABLE IF NOT EXISTS community_topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    forum_host TEXT NOT NULL,
    topic_id INTEGER NOT NULL,
    slug TEXT,
    title TEXT,
    url TEXT NOT NULL,
    category TEXT,
    author_username TEXT,
    created_at_src TEXT,
    posts_count_declared INTEGER,
    first_seen_at TEXT NOT NULL,
    last_ingested_at TEXT NOT NULL,
    UNIQUE(forum_host, topic_id)
);

CREATE TABLE IF NOT EXISTS community_authors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    forum_host TEXT NOT NULL,
    username TEXT NOT NULL,
    username_norm TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    UNIQUE(forum_host, username_norm)
);

CREATE TABLE IF NOT EXISTS community_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_row_id INTEGER NOT NULL REFERENCES community_topics(id) ON DELETE CASCADE,
    post_number INTEGER NOT NULL,
    post_id INTEGER,
    author_id INTEGER NOT NULL REFERENCES community_authors(id),
    body TEXT NOT NULL,
    body_format TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    created_at_src TEXT,
    updated_at_src TEXT,
    reply_to_post_number INTEGER,
    post_url TEXT NOT NULL,
    revision_count INTEGER NOT NULL DEFAULT 0,
    first_capture_id INTEGER REFERENCES community_captures(id),
    last_capture_id INTEGER REFERENCES community_captures(id),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(topic_row_id, post_number)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_community_posts_post_id
    ON community_posts(topic_row_id, post_id) WHERE post_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS community_post_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_row_id INTEGER NOT NULL REFERENCES community_posts(id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    body_format TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    updated_at_src TEXT,
    superseded_at TEXT NOT NULL,
    superseded_by_capture_id INTEGER REFERENCES community_captures(id)
);

CREATE INDEX IF NOT EXISTS idx_community_revisions_post ON community_post_revisions(post_row_id);

CREATE TABLE IF NOT EXISTS community_discovery_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    forum_host TEXT NOT NULL,
    kind TEXT NOT NULL,
    source_key TEXT NOT NULL,
    source_url TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    raw_payload TEXT NOT NULL,
    topics_in_capture INTEGER NOT NULL DEFAULT 0,
    new_topics INTEGER NOT NULL DEFAULT 0,
    captured_at TEXT NOT NULL,
    UNIQUE(forum_host, source_key, content_hash)
);

CREATE TABLE IF NOT EXISTS community_discovered_topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    forum_host TEXT NOT NULL,
    topic_id INTEGER NOT NULL,
    slug TEXT,
    title TEXT,
    url TEXT NOT NULL,
    category TEXT,
    posts_count_seen INTEGER,
    last_posted_at_seen TEXT,
    first_run_id INTEGER NOT NULL REFERENCES community_discovery_runs(id),
    last_run_id INTEGER NOT NULL REFERENCES community_discovery_runs(id),
    times_seen INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(forum_host, topic_id)
);

CREATE TABLE IF NOT EXISTS community_discovery_sightings (
    run_id INTEGER NOT NULL REFERENCES community_discovery_runs(id),
    discovered_topic_id INTEGER NOT NULL REFERENCES community_discovered_topics(id),
    posts_count INTEGER,
    last_posted_at TEXT,
    classification TEXT NOT NULL,
    PRIMARY KEY (run_id, discovered_topic_id)
);

CREATE TABLE IF NOT EXISTS community_discovery_checkpoints (
    forum_host TEXT NOT NULL,
    source_key TEXT NOT NULL,
    high_water_last_posted_at TEXT,
    last_run_id INTEGER NOT NULL REFERENCES community_discovery_runs(id),
    runs_count INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (forum_host, source_key)
);

CREATE TABLE IF NOT EXISTS community_user_replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    forum_host TEXT NOT NULL,
    username_norm TEXT NOT NULL,
    topic_id INTEGER NOT NULL,
    post_number INTEGER NOT NULL,
    created_at_src TEXT,
    like_count INTEGER,
    first_run_id INTEGER NOT NULL REFERENCES community_discovery_runs(id),
    UNIQUE(forum_host, username_norm, topic_id, post_number)
);

CREATE TABLE IF NOT EXISTS community_user_activity (
    forum_host TEXT NOT NULL,
    username_norm TEXT NOT NULL,
    username TEXT NOT NULL,
    stats_json TEXT NOT NULL,
    last_run_id INTEGER NOT NULL REFERENCES community_discovery_runs(id),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (forum_host, username_norm)
);
"""


class CommunityRuntimeUnsupportedError(Exception):
    error_code = "community_postgresql_runtime_unsupported"

    def __init__(self) -> None:
        self.message = (
            "Community capture is supported on the SQLite runtime only. "
            "PostgreSQL persistence is not implemented."
        )
        super().__init__(self.message)


def require_community_sqlite_runtime(store: Any):
    if getattr(store, "backend", "sqlite") != "sqlite":
        raise CommunityRuntimeUnsupportedError()
    return store.database_path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_community_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(COMMUNITY_SCHEMA)


def find_capture(conn: sqlite3.Connection, host: str, topic_id: int, content_hash: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM community_captures WHERE forum_host = ? AND topic_id = ? AND content_hash = ?",
        (host, topic_id, content_hash),
    ).fetchone()
    return dict(row) if row else None


def insert_capture(
    conn: sqlite3.Connection,
    *,
    host: str,
    topic_id: int,
    source_url: str,
    source_format: str,
    content_hash: str,
    raw_payload: str,
) -> int:
    cur = conn.execute(
        """INSERT INTO community_captures
           (forum_host, topic_id, source_url, source_format, content_hash, raw_payload, captured_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (host, topic_id, source_url, source_format, content_hash, raw_payload, _now()),
    )
    return int(cur.lastrowid)


def finish_capture(
    conn: sqlite3.Connection,
    capture_id: int,
    *,
    posts: int,
    new: int,
    edited: int,
    unchanged: int,
    errors: int,
) -> None:
    conn.execute(
        """UPDATE community_captures SET posts_in_capture = ?, new_posts = ?, edited_posts = ?,
           unchanged_posts = ?, parse_error_count = ? WHERE id = ?""",
        (posts, new, edited, unchanged, errors, capture_id),
    )


def upsert_topic(conn: sqlite3.Connection, topic: Any) -> int:
    now = _now()
    existing = conn.execute(
        "SELECT id FROM community_topics WHERE forum_host = ? AND topic_id = ?",
        (topic.forum_host, topic.topic_id),
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE community_topics SET
                 slug = COALESCE(?, slug), title = COALESCE(?, title), url = ?,
                 category = COALESCE(?, category), author_username = COALESCE(?, author_username),
                 created_at_src = COALESCE(?, created_at_src),
                 posts_count_declared = COALESCE(?, posts_count_declared),
                 last_ingested_at = ?
               WHERE id = ?""",
            (
                topic.slug, topic.title, topic.topic_url, topic.category, topic.author_username,
                topic.created_at, topic.posts_count_declared, now, existing["id"],
            ),
        )
        return int(existing["id"])
    cur = conn.execute(
        """INSERT INTO community_topics
           (forum_host, topic_id, slug, title, url, category, author_username, created_at_src,
            posts_count_declared, first_seen_at, last_ingested_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            topic.forum_host, topic.topic_id, topic.slug, topic.title, topic.topic_url,
            topic.category, topic.author_username, topic.created_at, topic.posts_count_declared, now, now,
        ),
    )
    return int(cur.lastrowid)


def get_or_create_author(conn: sqlite3.Connection, host: str, username: str) -> int:
    norm = username.strip().lower()
    row = conn.execute(
        "SELECT id FROM community_authors WHERE forum_host = ? AND username_norm = ?", (host, norm)
    ).fetchone()
    if row:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO community_authors (forum_host, username, username_norm, first_seen_at) VALUES (?, ?, ?, ?)",
        (host, username.strip(), norm, _now()),
    )
    return int(cur.lastrowid)


def get_post(conn: sqlite3.Connection, topic_row_id: int, post_number: int) -> Optional[dict]:
    row = conn.execute(
        """SELECT p.*, a.username_norm AS author_norm FROM community_posts p
           JOIN community_authors a ON a.id = p.author_id
           WHERE p.topic_row_id = ? AND p.post_number = ?""",
        (topic_row_id, post_number),
    ).fetchone()
    return dict(row) if row else None


def post_id_owner(conn: sqlite3.Connection, topic_row_id: int, post_id: int) -> Optional[int]:
    row = conn.execute(
        "SELECT post_number FROM community_posts WHERE topic_row_id = ? AND post_id = ?",
        (topic_row_id, post_id),
    ).fetchone()
    return int(row["post_number"]) if row else None


def insert_post(
    conn: sqlite3.Connection,
    *,
    topic_row_id: int,
    author_id: int,
    post: Any,
    body_hash: str,
    post_url: str,
    capture_id: int,
) -> int:
    now = _now()
    cur = conn.execute(
        """INSERT INTO community_posts
           (topic_row_id, post_number, post_id, author_id, body, body_format, body_hash,
            created_at_src, updated_at_src, reply_to_post_number, post_url,
            first_capture_id, last_capture_id, first_seen_at, last_seen_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            topic_row_id, post.post_number, post.post_id, author_id, post.body, post.body_format,
            body_hash, post.created_at, post.updated_at, post.reply_to_post_number, post_url,
            capture_id, capture_id, now, now,
        ),
    )
    return int(cur.lastrowid)


def touch_post(conn: sqlite3.Connection, existing: dict, post: Any, capture_id: int) -> None:
    """Record that an unchanged post was seen again; only fill fields that were unknown."""
    conn.execute(
        """UPDATE community_posts SET
             post_id = COALESCE(post_id, ?),
             created_at_src = COALESCE(created_at_src, ?),
             updated_at_src = COALESCE(?, updated_at_src),
             reply_to_post_number = COALESCE(reply_to_post_number, ?),
             last_capture_id = ?, last_seen_at = ?
           WHERE id = ?""",
        (post.post_id, post.created_at, post.updated_at, post.reply_to_post_number, capture_id, _now(), existing["id"]),
    )


def revise_post(
    conn: sqlite3.Connection, existing: dict, post: Any, body_hash: str, capture_id: int
) -> None:
    """Preserve the superseded body as a revision, then store the new body."""
    now = _now()
    conn.execute(
        """INSERT INTO community_post_revisions
           (post_row_id, body, body_format, body_hash, updated_at_src, superseded_at, superseded_by_capture_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            existing["id"], existing["body"], existing["body_format"], existing["body_hash"],
            existing["updated_at_src"], now, capture_id,
        ),
    )
    conn.execute(
        """UPDATE community_posts SET
             body = ?, body_format = ?, body_hash = ?,
             post_id = COALESCE(post_id, ?),
             created_at_src = COALESCE(created_at_src, ?),
             updated_at_src = COALESCE(?, updated_at_src),
             reply_to_post_number = COALESCE(reply_to_post_number, ?),
             revision_count = revision_count + 1, last_capture_id = ?, last_seen_at = ?
           WHERE id = ?""",
        (
            post.body, post.body_format, body_hash, post.post_id, post.created_at, post.updated_at,
            post.reply_to_post_number, capture_id, now, existing["id"],
        ),
    )


# ---------------------------------------------------------------- queries


def list_topics(conn: sqlite3.Connection, *, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        """SELECT t.*, COUNT(p.id) AS posts_stored, MAX(p.created_at_src) AS last_post_at
           FROM community_topics t LEFT JOIN community_posts p ON p.topic_row_id = t.id
           GROUP BY t.id ORDER BY t.last_ingested_at DESC, t.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_topic(conn: sqlite3.Connection, topic_row_id: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM community_topics WHERE id = ?", (topic_row_id,)).fetchone()
    return dict(row) if row else None


def list_posts(conn: sqlite3.Connection, topic_row_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT p.*, a.username AS author_username FROM community_posts p
           JOIN community_authors a ON a.id = p.author_id
           WHERE p.topic_row_id = ? ORDER BY p.post_number ASC""",
        (topic_row_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_revisions(conn: sqlite3.Connection, topic_row_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT r.*, p.post_number FROM community_post_revisions r
           JOIN community_posts p ON p.id = r.post_row_id
           WHERE p.topic_row_id = ? ORDER BY p.post_number ASC, r.id ASC""",
        (topic_row_id,),
    ).fetchall()
    return [dict(r) for r in rows]
