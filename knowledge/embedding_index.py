"""Chunk + embedding persistence and bounded indexing (Phase K1).

Additive schema only: two new tables referencing ``knowledge_items``. K0
items stay readable and lexically searchable whether or not they have chunks
or embeddings.

An embedding row is *valid* for search only when all of these hold:
``status='ok'``, its ``model`` equals the configured model, its ``fingerprint``
equals the chunk's current fingerprint, and its ``dimension`` equals the
model's established dimension. Anything else is stale, failed, or
incompatible and is never used for ranking.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import db
from knowledge.chunking import chunk_text
from knowledge.embeddings import (
    PROVIDER_UNAVAILABLE_CODES,
    EmbeddingError,
    EmbeddingRuntime,
    l2_normalize,
    pack_vector,
)

logger = logging.getLogger(__name__)

EMBEDDING_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES knowledge_items(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    char_start INTEGER NOT NULL,
    char_end INTEGER NOT NULL,
    text TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (item_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_item ON knowledge_chunks(item_id);

CREATE TABLE IF NOT EXISTS knowledge_chunk_embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk_id INTEGER NOT NULL REFERENCES knowledge_chunks(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    dimension INTEGER,
    vector BLOB,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok', 'failed')),
    error_code TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    indexed_at TEXT NOT NULL,
    UNIQUE (chunk_id, model)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_chunk_embeddings_model
    ON knowledge_chunk_embeddings(model, status);
"""

DEFAULT_MAX_ATTEMPTS = 3
MAX_REINDEX_LIMIT = 2000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_embedding_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(EMBEDDING_SCHEMA)


# --------------------------------------------------------------------- chunks


def sync_chunks(conn: sqlite3.Connection, item_id: int) -> dict[str, int]:
    """Bring an item's chunk rows in line with its current text.

    Unchanged chunks keep their row id (stable citations) and embeddings.
    Changed chunks get the new text/fingerprint; their old embeddings become
    stale by fingerprint mismatch and are re-embedded on the next index run.
    """
    row = conn.execute("SELECT normalized_text FROM knowledge_items WHERE id = ?", (item_id,)).fetchone()
    chunks = chunk_text(row["normalized_text"] or "") if row else []
    existing = {
        int(r["chunk_index"]): dict(r)
        for r in conn.execute(
            "SELECT id, chunk_index, fingerprint FROM knowledge_chunks WHERE item_id = ?", (item_id,)
        ).fetchall()
    }
    counts = {"created": 0, "changed": 0, "unchanged": 0, "removed": 0}
    now = _now()
    for chunk in chunks:
        current = existing.pop(chunk.index, None)
        if current is None:
            conn.execute(
                """
                INSERT INTO knowledge_chunks
                    (item_id, chunk_index, char_start, char_end, text, fingerprint, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (item_id, chunk.index, chunk.char_start, chunk.char_end, chunk.text, chunk.fingerprint, now, now),
            )
            counts["created"] += 1
        elif current["fingerprint"] != chunk.fingerprint:
            conn.execute(
                """
                UPDATE knowledge_chunks
                SET char_start = ?, char_end = ?, text = ?, fingerprint = ?, updated_at = ?
                WHERE id = ?
                """,
                (chunk.char_start, chunk.char_end, chunk.text, chunk.fingerprint, now, current["id"]),
            )
            counts["changed"] += 1
        else:
            counts["unchanged"] += 1
    for stale in existing.values():
        conn.execute("DELETE FROM knowledge_chunks WHERE id = ?", (stale["id"],))
        counts["removed"] += 1
    return counts


def model_dimension(conn: sqlite3.Connection, model: str) -> Optional[int]:
    """The dimension established by the earliest valid vector for ``model``."""
    row = conn.execute(
        """
        SELECT dimension FROM knowledge_chunk_embeddings
        WHERE model = ? AND status = 'ok' AND dimension IS NOT NULL
        ORDER BY id ASC LIMIT 1
        """,
        (model,),
    ).fetchone()
    return int(row["dimension"]) if row else None


def _record(
    conn: sqlite3.Connection,
    *,
    chunk_id: int,
    model: str,
    fingerprint: str,
    vector: Optional[list[float]],
    error_code: Optional[str],
) -> None:
    status = "ok" if vector is not None else "failed"
    conn.execute(
        """
        INSERT INTO knowledge_chunk_embeddings
            (chunk_id, model, dimension, vector, fingerprint, status, error_code, attempts, indexed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
        ON CONFLICT (chunk_id, model) DO UPDATE SET
            dimension = excluded.dimension,
            vector = excluded.vector,
            fingerprint = excluded.fingerprint,
            status = excluded.status,
            error_code = excluded.error_code,
            attempts = CASE
                WHEN knowledge_chunk_embeddings.fingerprint = excluded.fingerprint
                THEN knowledge_chunk_embeddings.attempts + 1 ELSE 1 END,
            indexed_at = excluded.indexed_at
        """,
        (
            chunk_id,
            model,
            len(vector) if vector is not None else None,
            pack_vector(vector) if vector is not None else None,
            fingerprint,
            status,
            error_code,
            _now(),
        ),
    )


# --------------------------------------------------------------------- indexing


@dataclass
class IndexReport:
    status: str
    model: Optional[str]
    items_considered: int = 0
    chunks_total: int = 0
    eligible: int = 0
    indexed: int = 0
    skipped_unchanged: int = 0
    skipped_max_attempts: int = 0
    failed: int = 0
    deferred: int = 0
    error_codes: dict[str, int] = field(default_factory=dict)
    disabled_reason: Optional[str] = None
    aborted_code: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _candidate_chunks(
    conn: sqlite3.Connection,
    *,
    model: str,
    item_ids: Optional[list[int]],
) -> list[dict[str, Any]]:
    where = ""
    params: list[Any] = [model]
    if item_ids is not None:
        where = f"WHERE c.item_id IN ({', '.join('?' for _ in item_ids)})"
        params.extend(item_ids)
    rows = conn.execute(
        f"""
        SELECT c.id AS chunk_id, c.item_id, c.text, c.fingerprint AS chunk_fp,
               e.status, e.fingerprint AS emb_fp, e.dimension, e.attempts
        FROM knowledge_chunks c
        LEFT JOIN knowledge_chunk_embeddings e ON e.chunk_id = c.id AND e.model = ?
        {where}
        ORDER BY c.id ASC
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def index_knowledge(
    database_path: Path,
    runtime: EmbeddingRuntime,
    *,
    item_ids: Optional[list[int]] = None,
    limit: int = 200,
    retry_failed: bool = False,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> IndexReport:
    """One bounded indexing pass. Never loops: each eligible chunk is tried at most once per call."""
    if not runtime.enabled:
        return IndexReport(status="disabled", model=runtime.model, disabled_reason=runtime.disabled_reason)
    model = str(runtime.model)
    limit = max(1, min(int(limit), MAX_REINDEX_LIMIT))
    report = IndexReport(status="ok", model=model)

    with db.get_conn(database_path) as conn:
        if item_ids is None:
            ids = [int(r[0]) for r in conn.execute("SELECT id FROM knowledge_items ORDER BY id").fetchall()]
        else:
            ids = sorted({int(i) for i in item_ids})
        for item_id in ids:
            sync_chunks(conn, item_id)
        report.items_considered = len(ids)
        rows = _candidate_chunks(conn, model=model, item_ids=ids)
        established_dim = model_dimension(conn, model)

    report.chunks_total = len(rows)
    eligible: list[dict[str, Any]] = []
    for row in rows:
        fingerprint_matches = row["emb_fp"] == row["chunk_fp"]
        if row["status"] == "ok" and fingerprint_matches and (
            established_dim is None or row["dimension"] == established_dim
        ):
            report.skipped_unchanged += 1
        elif (
            row["status"] == "failed"
            and fingerprint_matches
            and not retry_failed
            and int(row["attempts"] or 0) >= max_attempts
        ):
            report.skipped_max_attempts += 1
        else:
            eligible.append(row)
    report.eligible = len(eligible)
    batch_rows = eligible[:limit]
    report.deferred = len(eligible) - len(batch_rows)

    batch_size = runtime.settings.batch_size
    provider = runtime.provider
    assert provider is not None

    def fail(row: dict[str, Any], code: str, conn: sqlite3.Connection) -> None:
        _record(conn, chunk_id=row["chunk_id"], model=model, fingerprint=row["chunk_fp"], vector=None, error_code=code)
        report.failed += 1
        report.error_codes[code] = report.error_codes.get(code, 0) + 1

    def store_vectors(pairs: list[tuple[dict[str, Any], list[float]]], conn: sqlite3.Connection) -> None:
        nonlocal established_dim
        for row, raw in pairs:
            try:
                vector = l2_normalize(raw)
            except EmbeddingError as exc:
                fail(row, exc.code, conn)
                continue
            if established_dim is None:
                established_dim = len(vector)
            if len(vector) != established_dim:
                fail(row, "embedding_dimension_mismatch", conn)
                continue
            _record(conn, chunk_id=row["chunk_id"], model=model, fingerprint=row["chunk_fp"], vector=vector, error_code=None)
            report.indexed += 1

    for start in range(0, len(batch_rows), batch_size):
        batch = batch_rows[start : start + batch_size]
        try:
            vectors = provider.embed([row["text"] for row in batch])
            results: list[tuple[dict[str, Any], Optional[list[float]], Optional[str]]] = [
                (row, vec, None) for row, vec in zip(batch, vectors)
            ]
        except EmbeddingError as exc:
            if exc.code in PROVIDER_UNAVAILABLE_CODES or len(batch) == 1:
                results = [(row, None, exc.code) for row in batch]
            else:
                # Content-specific failure: isolate the bad chunk(s), one bounded call each.
                results = []
                for row in batch:
                    try:
                        results.append((row, provider.embed([row["text"]])[0], None))
                    except EmbeddingError as inner:
                        results.append((row, None, inner.code))
        with db.get_conn(database_path) as conn:
            store_vectors([(r, v) for r, v, c in results if v is not None], conn)
            for row, _vec, code in results:
                if code is not None:
                    fail(row, code, conn)
        unavailable = next((c for _r, _v, c in results if c in PROVIDER_UNAVAILABLE_CODES), None)
        if unavailable:
            # Provider is down: stop this pass instead of hammering it.
            report.aborted_code = unavailable
            report.deferred += len(batch_rows) - (start + len(batch))
            logger.warning("embedding indexing aborted: %s", unavailable)
            break

    if report.failed and report.indexed:
        report.status = "partial"
    elif report.failed:
        report.status = "failed"
    return report


# --------------------------------------------------------------------- status


def embedding_status(
    conn: sqlite3.Connection,
    runtime: EmbeddingRuntime,
    *,
    item_id: Optional[int] = None,
) -> dict[str, Any]:
    """Counts for the configured model. Read-only; does not create chunks."""
    model = runtime.model
    item_clause = "WHERE c.item_id = ?" if item_id is not None else ""
    params: list[Any] = [model or ""]
    if item_id is not None:
        params.append(item_id)
    dim = model_dimension(conn, model) if model else None
    rows = conn.execute(
        f"""
        SELECT c.item_id, e.status, e.error_code, e.dimension,
               (e.fingerprint = c.fingerprint) AS fp_ok
        FROM knowledge_chunks c
        LEFT JOIN knowledge_chunk_embeddings e ON e.chunk_id = c.id AND e.model = ?
        {item_clause}
        """,
        params,
    ).fetchall()
    counts = {"chunks_total": len(rows), "embedded": 0, "stale": 0, "failed": 0, "missing": 0, "incompatible": 0}
    errors: dict[str, int] = {}
    embedded_by_item: dict[int, list[bool]] = {}
    for r in rows:
        ok = False
        if r["status"] is None:
            counts["missing"] += 1
        elif r["status"] == "failed":
            counts["failed"] += 1
            errors[r["error_code"] or "unknown"] = errors.get(r["error_code"] or "unknown", 0) + 1
        elif not r["fp_ok"]:
            counts["stale"] += 1
        elif dim is not None and r["dimension"] != dim:
            counts["incompatible"] += 1
        else:
            counts["embedded"] += 1
            ok = True
        embedded_by_item.setdefault(int(r["item_id"]), []).append(ok)

    items_q = "SELECT COUNT(*) FROM knowledge_items" + (" WHERE id = ?" if item_id is not None else "")
    items_total = int(conn.execute(items_q, [item_id] if item_id is not None else []).fetchone()[0])
    text_q = (
        "SELECT COUNT(*) FROM knowledge_items WHERE normalized_text IS NOT NULL AND normalized_text != ''"
        + (" AND id = ?" if item_id is not None else "")
    )
    items_with_text = int(conn.execute(text_q, [item_id] if item_id is not None else []).fetchone()[0])
    other_q = "SELECT COUNT(*) FROM knowledge_chunk_embeddings WHERE model != ?"
    other_models = int(conn.execute(other_q, (model or "",)).fetchone()[0])
    last = conn.execute(
        "SELECT MAX(indexed_at) FROM knowledge_chunk_embeddings WHERE model = ?", (model or "",)
    ).fetchone()[0]
    return {
        "runtime": runtime.describe(),
        "model": model,
        "dimension": dim,
        "item_id": item_id,
        "items_total": items_total,
        "items_with_text": items_with_text,
        "items_chunked": len(embedded_by_item),
        "items_fully_embedded": sum(1 for flags in embedded_by_item.values() if flags and all(flags)),
        **counts,
        "error_codes": errors,
        "other_model_embeddings": other_models,
        "last_indexed_at": last,
    }


def reindex_with_audit(
    database_path: Path,
    runtime: EmbeddingRuntime,
    *,
    item_ids: Optional[list[int]] = None,
    limit: int = 200,
    retry_failed: bool = False,
    command_log_store: Any = None,
) -> tuple[IndexReport, str]:
    """Run one bounded indexing pass and record it in the command log (counts only)."""
    # services must load before repositories.command_log_store (existing import cycle).
    from services.command_log import CommandStatus, transition
    from repositories.command_log_store import SqliteCommandLogStore

    command_log = command_log_store or SqliteCommandLogStore(database_path)
    entry = command_log.create("Knowledge embedding re-index")
    entry.intent = "knowledge_reindex"
    entry.tool_name = "knowledge_reindex"
    entry.risk_class = "write"
    entry.tool_arguments = {
        "model": runtime.model,
        "limit": limit,
        "retry_failed": retry_failed,
        "item_ids": item_ids,
    }
    transition(entry, CommandStatus.PLANNED)
    command_log.update(entry)
    transition(entry, CommandStatus.EXECUTING)
    command_log.update(entry)
    try:
        report = index_knowledge(
            database_path, runtime, item_ids=item_ids, limit=limit, retry_failed=retry_failed
        )
    except Exception:
        logger.exception("knowledge re-index failed")
        transition(entry, CommandStatus.FAILED)
        entry.error_code = "knowledge_reindex_failed"
        entry.error_message = "Re-index failed unexpectedly."
        command_log.update(entry)
        raise
    if report.status in {"disabled", "failed"}:
        transition(entry, CommandStatus.FAILED)
        entry.error_code = report.disabled_reason or report.aborted_code or "knowledge_reindex_failed"
        entry.error_message = "No embeddings were stored."
    else:
        transition(entry, CommandStatus.SUCCEEDED)
    entry.result_summary = report.to_dict()
    command_log.update(entry)
    return report, str(entry.id)
