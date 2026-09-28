"""SQLite persistence and retrieval for knowledge items.

All reads and writes go through these typed functions; callers own the
connection/transaction (``db.get_conn``). Schema creation is additive
(``CREATE ... IF NOT EXISTS``) and runs from ``db.init_db`` only.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from knowledge.schemas import (
    ContentKind,
    EntityType,
    KnowledgeClassification,
    KnowledgeSearchFilters,
    LinkStatus,
)

KNOWLEDGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash TEXT NOT NULL UNIQUE,
    source_path TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    content_kind TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    raw_text TEXT,
    normalized_text TEXT,
    extraction_method TEXT,
    extraction_status TEXT NOT NULL,
    vision_status TEXT NOT NULL DEFAULT 'not_applicable',
    vision_model TEXT,
    visual_description TEXT,
    summary TEXT,
    project TEXT,
    project_source TEXT,
    category TEXT,
    sub_category TEXT,
    event_date TEXT,
    event_date_source TEXT,
    captured_at TEXT NOT NULL,
    importance REAL,
    classification_status TEXT NOT NULL,
    classification_model TEXT,
    classification_warning TEXT,
    metadata_json TEXT,
    ingest_command_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_knowledge_items_captured_at ON knowledge_items(captured_at);
CREATE INDEX IF NOT EXISTS idx_knowledge_items_event_date ON knowledge_items(event_date);
CREATE INDEX IF NOT EXISTS idx_knowledge_items_project ON knowledge_items(project COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_knowledge_items_category ON knowledge_items(category COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS knowledge_topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS knowledge_item_topics (
    item_id INTEGER NOT NULL REFERENCES knowledge_items(id) ON DELETE CASCADE,
    topic_id INTEGER NOT NULL REFERENCES knowledge_topics(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    PRIMARY KEY (item_id, topic_id)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_item_topics_topic ON knowledge_item_topics(topic_id);

CREATE TABLE IF NOT EXISTS knowledge_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    UNIQUE (normalized_name, entity_type)
);

CREATE TABLE IF NOT EXISTS knowledge_item_entities (
    item_id INTEGER NOT NULL REFERENCES knowledge_items(id) ON DELETE CASCADE,
    entity_id INTEGER NOT NULL REFERENCES knowledge_entities(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    link_status TEXT NOT NULL,
    person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
    lead_id INTEGER REFERENCES leads(id) ON DELETE SET NULL,
    PRIMARY KEY (item_id, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_item_entities_entity ON knowledge_item_entities(entity_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_item_entities_person ON knowledge_item_entities(person_id);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    original_filename,
    summary,
    normalized_text,
    topics,
    entities,
    project,
    category,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""

# bm25 weights, in FTS column order.
_FTS_WEIGHTS = (2.0, 3.0, 1.0, 3.0, 3.0, 2.0, 2.0)
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_label(value: str) -> str:
    return _WS_RE.sub(" ", (value or "").strip().lower())


def _json_dumps(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _json_loads(value: Optional[str], default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def ensure_knowledge_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(KNOWLEDGE_SCHEMA)
    try:
        conn.executescript(FTS_SCHEMA)
    except sqlite3.OperationalError:
        # SQLite built without FTS5: search degrades to LIKE matching.
        pass
    # Phase K1 (additive): chunk and embedding tables.
    from knowledge.embedding_index import ensure_embedding_tables

    ensure_embedding_tables(conn)


def fts_available(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'knowledge_fts'"
    ).fetchone()
    return row is not None


# --------------------------------------------------------------------------- writes


def find_item_by_hash(conn: sqlite3.Connection, content_hash: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM knowledge_items WHERE content_hash = ?", (content_hash,)
    ).fetchone()
    return dict(row) if row else None


def insert_item(conn: sqlite3.Connection, values: dict[str, Any]) -> int:
    now = _now()
    record = {
        **values,
        "metadata_json": _json_dumps(values.get("metadata_json")),
        "created_at": now,
        "updated_at": now,
    }
    columns = list(record)
    placeholders = ", ".join("?" for _ in columns)
    cursor = conn.execute(
        f"INSERT INTO knowledge_items ({', '.join(columns)}) VALUES ({placeholders})",
        [record[c] for c in columns],
    )
    return int(cursor.lastrowid)


def _upsert_topic(conn: sqlite3.Connection, name: str) -> int:
    norm = normalize_label(name)
    conn.execute(
        "INSERT OR IGNORE INTO knowledge_topics (name, normalized_name) VALUES (?, ?)",
        (name, norm),
    )
    row = conn.execute("SELECT id FROM knowledge_topics WHERE normalized_name = ?", (norm,)).fetchone()
    return int(row["id"])


def _upsert_entity(conn: sqlite3.Connection, name: str, entity_type: EntityType) -> int:
    norm = normalize_label(name)
    conn.execute(
        "INSERT OR IGNORE INTO knowledge_entities (name, normalized_name, entity_type) VALUES (?, ?, ?)",
        (name, norm, entity_type.value),
    )
    row = conn.execute(
        "SELECT id FROM knowledge_entities WHERE normalized_name = ? AND entity_type = ?",
        (norm, entity_type.value),
    ).fetchone()
    return int(row["id"])


def _person_link(conn: sqlite3.Connection, name: str) -> tuple[LinkStatus, Optional[int]]:
    """Link only on an exact (case/whitespace-insensitive) unique name match."""
    import db

    target = db.normalize_name(name)
    if not target or " " not in target:
        # Single-token names ("Nikola") are too ambiguous to link.
        return LinkStatus.UNLINKED, None
    first = target.split(" ")[0]
    rows = conn.execute(
        "SELECT id, name FROM people WHERE name IS NOT NULL AND lower(name) LIKE ?",
        (f"%{first}%",),
    ).fetchall()
    matches = sorted({int(r["id"]) for r in rows if db.normalize_name(r["name"]) == target})
    if len(matches) == 1:
        return LinkStatus.LINKED, matches[0]
    if len(matches) > 1:
        return LinkStatus.AMBIGUOUS, None
    return LinkStatus.UNLINKED, None


def _organization_link(conn: sqlite3.Connection, name: str) -> tuple[LinkStatus, Optional[int]]:
    import db

    target = db.normalize_name(name)
    if not target:
        return LinkStatus.UNLINKED, None
    rows = conn.execute("SELECT id FROM leads WHERE normalized_name = ?", (target,)).fetchall()
    ids = sorted({int(r["id"]) for r in rows})
    if len(ids) == 1:
        return LinkStatus.LINKED, ids[0]
    if len(ids) > 1:
        return LinkStatus.AMBIGUOUS, None
    return LinkStatus.UNLINKED, None


def attach_classification(
    conn: sqlite3.Connection,
    item_id: int,
    classification: KnowledgeClassification,
    *,
    source: str = "llm",
) -> None:
    for topic in classification.topics:
        topic_id = _upsert_topic(conn, topic)
        conn.execute(
            "INSERT OR IGNORE INTO knowledge_item_topics (item_id, topic_id, source) VALUES (?, ?, ?)",
            (item_id, topic_id, source),
        )
    for entity in classification.entities:
        entity_id = _upsert_entity(conn, entity.name, entity.type)
        person_id: Optional[int] = None
        lead_id: Optional[int] = None
        if entity.type == EntityType.PERSON:
            status, person_id = _person_link(conn, entity.name)
        elif entity.type == EntityType.ORGANIZATION:
            status, lead_id = _organization_link(conn, entity.name)
        else:
            status = LinkStatus.NOT_APPLICABLE
        conn.execute(
            """
            INSERT OR IGNORE INTO knowledge_item_entities
                (item_id, entity_id, source, link_status, person_id, lead_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (item_id, entity_id, source, status.value, person_id, lead_id),
        )


def item_topics(conn: sqlite3.Connection, item_id: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT t.name FROM knowledge_item_topics it
        JOIN knowledge_topics t ON t.id = it.topic_id
        WHERE it.item_id = ? ORDER BY t.name
        """,
        (item_id,),
    ).fetchall()
    return [r["name"] for r in rows]


def item_entities(conn: sqlite3.Connection, item_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT e.id AS entity_id, e.name, e.entity_type, ie.link_status, ie.person_id, ie.lead_id,
               p.name AS person_name, l.company_name AS lead_name
        FROM knowledge_item_entities ie
        JOIN knowledge_entities e ON e.id = ie.entity_id
        LEFT JOIN people p ON p.id = ie.person_id
        LEFT JOIN leads l ON l.id = ie.lead_id
        WHERE ie.item_id = ? ORDER BY e.entity_type, e.name
        """,
        (item_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def refresh_fts(conn: sqlite3.Connection, item_id: int) -> None:
    if not fts_available(conn):
        return
    row = conn.execute(
        "SELECT original_filename, summary, normalized_text, project, category, sub_category "
        "FROM knowledge_items WHERE id = ?",
        (item_id,),
    ).fetchone()
    conn.execute("DELETE FROM knowledge_fts WHERE rowid = ?", (item_id,))
    if row is None:
        return
    topics = " ; ".join(item_topics(conn, item_id))
    entities = " ; ".join(e["name"] for e in item_entities(conn, item_id))
    category = " ".join(x for x in (row["category"], row["sub_category"]) if x)
    conn.execute(
        """
        INSERT INTO knowledge_fts
            (rowid, original_filename, summary, normalized_text, topics, entities, project, category)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item_id,
            row["original_filename"],
            row["summary"] or "",
            row["normalized_text"] or "",
            topics,
            entities,
            row["project"] or "",
            category,
        ),
    )


# --------------------------------------------------------------------------- reads


def derive_status(row: dict[str, Any]) -> str:
    if row.get("extraction_status") == "failed":
        return "extraction_failed"
    if row.get("extraction_status") == "needs_vision" and row.get("vision_status") != "ok":
        return "needs_vision"
    if row.get("classification_status") != "ok":
        return "unclassified"
    return "ready"


def to_public_item(conn: sqlite3.Connection, row: dict[str, Any], *, include_text: bool = False) -> dict[str, Any]:
    item_id = int(row["id"])
    out = {
        "id": item_id,
        "original_filename": row["original_filename"],
        "source_path": row["source_path"],
        "mime_type": row["mime_type"],
        "content_kind": row["content_kind"],
        "size_bytes": row["size_bytes"],
        "content_hash": row["content_hash"],
        "captured_at": row["captured_at"],
        "event_date": row["event_date"],
        "event_date_source": row["event_date_source"],
        "summary": row["summary"],
        "project": row["project"],
        "project_source": row["project_source"],
        "category": row["category"],
        "sub_category": row["sub_category"],
        "importance": row["importance"],
        "extraction_status": row["extraction_status"],
        "extraction_method": row["extraction_method"],
        "vision_status": row["vision_status"],
        "classification_status": row["classification_status"],
        "classification_model": row["classification_model"],
        "classification_warning": row["classification_warning"],
        "status": derive_status(row),
        "topics": item_topics(conn, item_id),
        "entities": item_entities(conn, item_id),
        "metadata": _json_loads(row.get("metadata_json"), {}),
        "ingest_command_id": row["ingest_command_id"],
        "text_chars": len(row.get("normalized_text") or ""),
    }
    if include_text:
        out["raw_text"] = row.get("raw_text")
        out["normalized_text"] = row.get("normalized_text")
        out["visual_description"] = row.get("visual_description")
    return out


def get_item(conn: sqlite3.Connection, item_id: int, *, include_text: bool = True) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM knowledge_items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        return None
    return to_public_item(conn, dict(row), include_text=include_text)


def _filter_clauses(filters: KnowledgeSearchFilters) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if filters.project:
        clauses.append("ki.project = ? COLLATE NOCASE")
        params.append(filters.project.strip())
    if filters.category:
        clauses.append("(ki.category = ? COLLATE NOCASE OR ki.sub_category = ? COLLATE NOCASE)")
        params.extend([filters.category.strip(), filters.category.strip()])
    if filters.topic:
        clauses.append(
            "EXISTS (SELECT 1 FROM knowledge_item_topics it JOIN knowledge_topics t ON t.id = it.topic_id "
            "WHERE it.item_id = ki.id AND t.normalized_name = ?)"
        )
        params.append(normalize_label(filters.topic))
    if filters.entity:
        clauses.append(
            "EXISTS (SELECT 1 FROM knowledge_item_entities ie JOIN knowledge_entities e ON e.id = ie.entity_id "
            "WHERE ie.item_id = ki.id AND e.normalized_name = ?)"
        )
        params.append(normalize_label(filters.entity))
    if filters.person_id is not None:
        clauses.append(
            "EXISTS (SELECT 1 FROM knowledge_item_entities ie WHERE ie.item_id = ki.id AND ie.person_id = ?)"
        )
        params.append(filters.person_id)
    if filters.content_kind is not None:
        clauses.append("ki.content_kind = ?")
        params.append(filters.content_kind.value if isinstance(filters.content_kind, ContentKind) else filters.content_kind)
    if filters.event_from:
        clauses.append("ki.event_date >= ?")
        params.append(filters.event_from.isoformat())
    if filters.event_to:
        clauses.append("ki.event_date <= ?")
        params.append(filters.event_to.isoformat())
    if filters.captured_from:
        clauses.append("substr(ki.captured_at, 1, 10) >= ?")
        params.append(filters.captured_from.isoformat())
    if filters.captured_to:
        clauses.append("substr(ki.captured_at, 1, 10) <= ?")
        params.append(filters.captured_to.isoformat())
    if filters.date_from:
        clauses.append("COALESCE(ki.event_date, substr(ki.captured_at, 1, 10)) >= ?")
        params.append(filters.date_from.isoformat())
    if filters.date_to:
        clauses.append("COALESCE(ki.event_date, substr(ki.captured_at, 1, 10)) <= ?")
        params.append(filters.date_to.isoformat())
    return clauses, params


def query_tokens(query: str) -> list[str]:
    tokens = [t for t in _TOKEN_RE.findall(query or "") if t.strip("_")]
    return tokens[:16]


def build_fts_query(tokens: list[str], *, mode: str) -> str:
    parts = []
    for token in tokens:
        escaped = token.replace('"', '""')
        parts.append(f'"{escaped}"*' if len(token) >= 4 else f'"{escaped}"')
    return (" OR " if mode == "any" else " ").join(parts)


def _fallback_snippet(row: dict[str, Any], tokens: list[str], width: int = 220) -> str:
    text = row.get("normalized_text") or row.get("summary") or ""
    lower = text.lower()
    pos = -1
    for token in tokens:
        pos = lower.find(token.lower())
        if pos >= 0:
            break
    if pos < 0:
        snippet = (row.get("summary") or text)[:width]
    else:
        start = max(0, pos - width // 3)
        snippet = text[start : start + width]
        if start > 0:
            snippet = "… " + snippet
    return _WS_RE.sub(" ", snippet).strip()


def search_items(
    conn: sqlite3.Connection,
    *,
    query: str,
    filters: KnowledgeSearchFilters,
    limit: int = 10,
) -> dict[str, Any]:
    """Deterministic retrieval. Returns hits with snippets and ranking info."""
    tokens = query_tokens(query)
    clauses, params = _filter_clauses(filters)
    where_filters = (" AND " + " AND ".join(clauses)) if clauses else ""
    rows: list[dict[str, Any]] = []
    match_mode = "filters_only"
    engine = "none"

    if tokens and fts_available(conn):
        engine = "fts5_bm25"
        weights = ", ".join(str(w) for w in _FTS_WEIGHTS)
        for mode in ("all", "any"):
            if mode == "any" and len(tokens) < 2:
                break
            fts_query = build_fts_query(tokens, mode=mode)
            rows = [
                dict(r)
                for r in conn.execute(
                    f"""
                    SELECT ki.*, bm25(knowledge_fts, {weights}) AS rank_score,
                           snippet(knowledge_fts, -1, '[', ']', ' … ', 18) AS snippet
                    FROM knowledge_fts
                    JOIN knowledge_items ki ON ki.id = knowledge_fts.rowid
                    WHERE knowledge_fts MATCH ?{where_filters}
                    ORDER BY rank_score ASC, ki.captured_at DESC
                    LIMIT ?
                    """,
                    [fts_query, *params, limit],
                ).fetchall()
            ]
            match_mode = "all_terms" if mode == "all" else "any_term"
            if rows:
                break
    elif tokens:
        engine = "like_fallback"
        match_mode = "any_term"
        like_clauses = []
        like_params: list[Any] = []
        for token in tokens:
            like_clauses.append(
                "(ki.normalized_text LIKE ? OR ki.summary LIKE ? OR ki.original_filename LIKE ? "
                "OR ki.project LIKE ? OR ki.category LIKE ?)"
            )
            like_params.extend([f"%{token}%"] * 5)
        rows = [
            dict(r)
            for r in conn.execute(
                f"""
                SELECT ki.*, NULL AS rank_score, NULL AS snippet FROM knowledge_items ki
                WHERE ({' OR '.join(like_clauses)}){where_filters}
                ORDER BY ki.captured_at DESC LIMIT ?
                """,
                [*like_params, *params, limit],
            ).fetchall()
        ]
    else:
        engine = "metadata"
        rows = [
            dict(r)
            for r in conn.execute(
                f"""
                SELECT ki.*, NULL AS rank_score, NULL AS snippet FROM knowledge_items ki
                WHERE 1=1{where_filters}
                ORDER BY ki.captured_at DESC LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
        ]

    hits = []
    for position, row in enumerate(rows, start=1):
        item = to_public_item(conn, row)
        snippet = row.get("snippet") or _fallback_snippet(row, tokens)
        item["snippet"] = _WS_RE.sub(" ", snippet).strip()
        item["ranking"] = {
            "position": position,
            "engine": engine,
            "match_mode": match_mode,
            # bm25 is lower-is-better; expose a higher-is-better score.
            "score": round(-float(row["rank_score"]), 4) if row.get("rank_score") is not None else None,
        }
        item["source"] = {
            "knowledge_item_id": item["id"],
            "original_filename": item["original_filename"],
            "original_url": f"/knowledge/items/{item['id']}/original",
            "captured_at": item["captured_at"],
            "event_date": item["event_date"],
        }
        hits.append(item)
    return {
        "query": query,
        "tokens": tokens,
        "engine": engine,
        "match_mode": match_mode,
        "filters": filters.model_dump(mode="json", exclude_none=True),
        "count": len(hits),
        "hits": hits,
    }


def list_recent(conn: sqlite3.Connection, *, limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    total = int(conn.execute("SELECT COUNT(*) FROM knowledge_items").fetchone()[0])
    rows = conn.execute(
        "SELECT * FROM knowledge_items ORDER BY captured_at DESC, id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return [to_public_item(conn, dict(r)) for r in rows], total


def facet_counts(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    """Metadata-derived virtual folders: no files are duplicated."""

    def pairs(sql: str) -> list[dict[str, Any]]:
        return [{"value": r[0], "count": int(r[1])} for r in conn.execute(sql).fetchall() if r[0]]

    return {
        "projects": pairs(
            "SELECT project, COUNT(*) FROM knowledge_items WHERE project IS NOT NULL "
            "GROUP BY project COLLATE NOCASE ORDER BY COUNT(*) DESC LIMIT 30"
        ),
        "categories": pairs(
            "SELECT category, COUNT(*) FROM knowledge_items WHERE category IS NOT NULL "
            "GROUP BY category COLLATE NOCASE ORDER BY COUNT(*) DESC LIMIT 30"
        ),
        "topics": pairs(
            "SELECT t.name, COUNT(*) FROM knowledge_item_topics it JOIN knowledge_topics t ON t.id = it.topic_id "
            "GROUP BY t.id ORDER BY COUNT(*) DESC LIMIT 30"
        ),
        "months": pairs(
            "SELECT substr(COALESCE(event_date, captured_at), 1, 7), COUNT(*) FROM knowledge_items "
            "GROUP BY 1 ORDER BY 1 DESC LIMIT 24"
        ),
    }


def related_items(conn: sqlite3.Connection, item_id: int, *, limit: int = 5) -> list[dict[str, Any]]:
    """Items sharing topics or entities, ranked by overlap count (deterministic)."""
    rows = conn.execute(
        """
        SELECT other_id, SUM(weight) AS overlap FROM (
            SELECT b.item_id AS other_id, 1 AS weight
            FROM knowledge_item_topics a JOIN knowledge_item_topics b
              ON a.topic_id = b.topic_id AND b.item_id != a.item_id
            WHERE a.item_id = ?
            UNION ALL
            SELECT b.item_id AS other_id, 2 AS weight
            FROM knowledge_item_entities a JOIN knowledge_item_entities b
              ON a.entity_id = b.entity_id AND b.item_id != a.item_id
            WHERE a.item_id = ?
        ) GROUP BY other_id ORDER BY overlap DESC, other_id DESC LIMIT ?
        """,
        (item_id, item_id, limit),
    ).fetchall()
    out = []
    for r in rows:
        row = conn.execute(
            "SELECT id, original_filename, summary, captured_at, category FROM knowledge_items WHERE id = ?",
            (r["other_id"],),
        ).fetchone()
        if row:
            out.append({**dict(row), "overlap": int(r["overlap"])})
    return out


def iter_all_item_ids(conn: sqlite3.Connection) -> Iterable[int]:
    return [int(r[0]) for r in conn.execute("SELECT id FROM knowledge_items").fetchall()]
