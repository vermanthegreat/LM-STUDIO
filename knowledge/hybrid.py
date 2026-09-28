"""Semantic search and hybrid lexical/semantic fusion (Phase K1).

Semantic: cosine similarity between the L2-normalized query vector and stored
L2-normalized chunk vectors (dot product), restricted to valid embeddings of
the configured model and dimension. Each item is represented by its best
chunk. Ordering: score desc, then item id asc, then chunk index asc.

Hybrid: Reciprocal Rank Fusion, ``rrf = sum(1 / (RRF_K + rank))`` over the
lexical (K0 FTS5) and semantic rankings, deduplicated per knowledge item.
Ties break by best single-list rank, then item id asc.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Optional

from knowledge import repository as repo
from knowledge.embeddings import EmbeddingError, EmbeddingRuntime, l2_normalize, unpack_vector
from knowledge.embedding_index import model_dimension
from knowledge.schemas import KnowledgeSearchFilters

logger = logging.getLogger(__name__)

RRF_K = 60
SNIPPET_CHARS = 240


def semantic_search(
    conn: sqlite3.Connection,
    *,
    query_vector: list[float],
    model: str,
    filters: KnowledgeSearchFilters,
    limit: int,
    min_score: float,
    max_candidates: int,
) -> dict[str, Any]:
    dim = model_dimension(conn, model)
    if dim is None:
        return {"status": "no_index", "hits": [], "candidates": 0, "truncated": False}
    if len(query_vector) != dim:
        raise EmbeddingError(
            "embedding_dimension_mismatch",
            f"Query vector has {len(query_vector)} dimensions; index for model has {dim}.",
        )
    clauses, params = repo._filter_clauses(filters)
    where = (" AND " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"""
        SELECT c.id AS chunk_id, c.item_id, c.chunk_index, c.char_start, c.char_end, c.text, e.vector
        FROM knowledge_chunk_embeddings e
        JOIN knowledge_chunks c ON c.id = e.chunk_id
        JOIN knowledge_items ki ON ki.id = c.item_id
        WHERE e.model = ? AND e.status = 'ok' AND e.dimension = ? AND e.fingerprint = c.fingerprint{where}
        ORDER BY c.id ASC
        LIMIT ?
        """,
        [model, dim, *params, max_candidates + 1],
    ).fetchall()
    truncated = len(rows) > max_candidates
    rows = rows[:max_candidates]

    best: dict[int, dict[str, Any]] = {}
    for row in rows:
        vector = unpack_vector(row["vector"])
        score = sum(q * v for q, v in zip(query_vector, vector))
        if score < min_score:
            continue
        item_id = int(row["item_id"])
        current = best.get(item_id)
        if current is None or score > current["score"] or (
            score == current["score"] and row["chunk_index"] < current["chunk_index"]
        ):
            best[item_id] = {
                "item_id": item_id,
                "chunk_id": int(row["chunk_id"]),
                "chunk_index": int(row["chunk_index"]),
                "char_start": int(row["char_start"]),
                "char_end": int(row["char_end"]),
                "text": row["text"],
                "score": round(float(score), 6),
            }
    ordered = sorted(best.values(), key=lambda h: (-h["score"], h["item_id"], h["chunk_index"]))
    return {"status": "ok", "hits": ordered[:limit], "candidates": len(rows), "truncated": truncated}


def _chunk_ref(hit: dict[str, Any]) -> dict[str, Any]:
    return {
        "chunk_id": hit["chunk_id"],
        "chunk_index": hit["chunk_index"],
        "char_start": hit["char_start"],
        "char_end": hit["char_end"],
    }


def _item_hit(conn: sqlite3.Connection, item_id: int) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM knowledge_items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        return None
    item = repo.to_public_item(conn, dict(row))
    item["source"] = {
        "knowledge_item_id": item["id"],
        "original_filename": item["original_filename"],
        "original_url": f"/knowledge/items/{item['id']}/original",
        "captured_at": item["captured_at"],
        "event_date": item["event_date"],
    }
    return item


def _attach_chunk(hit: dict[str, Any], sem: dict[str, Any]) -> None:
    hit["source"]["chunk_id"] = sem["chunk_id"]
    hit["source"]["chunk_index"] = sem["chunk_index"]
    hit["retrieval"]["chunk"] = {**_chunk_ref(sem), "text": sem["text"]}
    hit["retrieval"]["semantic_score"] = sem["score"]


def fuse(
    conn: sqlite3.Connection,
    *,
    lexical_hits: list[dict[str, Any]],
    semantic_hits: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion, one result per knowledge item."""
    entries: dict[int, dict[str, Any]] = {}
    for rank, hit in enumerate(lexical_hits, start=1):
        entries[hit["id"]] = {"lexical_rank": rank, "semantic_rank": None, "lexical": hit, "semantic": None}
    for rank, sem in enumerate(semantic_hits, start=1):
        entry = entries.setdefault(
            sem["item_id"], {"lexical_rank": None, "semantic_rank": None, "lexical": None, "semantic": None}
        )
        entry["semantic_rank"] = rank
        entry["semantic"] = sem

    scored = []
    for item_id, entry in entries.items():
        ranks = [r for r in (entry["lexical_rank"], entry["semantic_rank"]) if r is not None]
        rrf = sum(1.0 / (RRF_K + r) for r in ranks)
        scored.append((round(rrf, 10), min(ranks), item_id, entry))
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))

    fused: list[dict[str, Any]] = []
    for rrf, _best, item_id, entry in scored:
        if len(fused) >= limit:
            break
        hit = entry["lexical"] or _item_hit(conn, item_id)
        if hit is None:  # row vanished between queries; never return it
            continue
        hit = dict(hit)
        hit["source"] = dict(hit["source"])
        evidence = [name for name in ("lexical", "semantic") if entry[f"{name}_rank"] is not None]
        hit["retrieval"] = {
            "evidence": evidence,
            "lexical_rank": entry["lexical_rank"],
            "semantic_rank": entry["semantic_rank"],
            "rrf_score": rrf,
        }
        if entry["semantic"] is not None:
            _attach_chunk(hit, entry["semantic"])
        if not hit.get("snippet") or entry["lexical"] is None:
            hit["snippet"] = " ".join((entry["semantic"] or {}).get("text", "").split())[:SNIPPET_CHARS]
        hit["ranking"] = {
            "position": len(fused) + 1,
            "engine": "hybrid_rrf",
            "match_mode": "+".join(evidence),
            "score": rrf,
        }
        fused.append(hit)
    return fused


def semantic_only(conn: sqlite3.Connection, semantic_hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for sem in semantic_hits:
        hit = _item_hit(conn, sem["item_id"])
        if hit is None:
            continue
        hit["retrieval"] = {"evidence": ["semantic"], "lexical_rank": None, "semantic_rank": len(out) + 1}
        _attach_chunk(hit, sem)
        hit["snippet"] = " ".join(sem["text"].split())[:SNIPPET_CHARS]
        hit["ranking"] = {
            "position": len(out) + 1,
            "engine": "semantic_cosine",
            "match_mode": "semantic",
            "score": sem["score"],
        }
        out.append(hit)
    return out


def embed_query(runtime: EmbeddingRuntime, query: str) -> list[float]:
    """One provider call, no retry within a request."""
    if not runtime.enabled or runtime.provider is None:
        raise EmbeddingError("embedding_disabled", runtime.disabled_reason or "embeddings_disabled")
    vectors = runtime.provider.embed([query])
    if len(vectors) != 1:
        raise EmbeddingError("embedding_count_mismatch", "Expected exactly one query vector.")
    return l2_normalize(vectors[0])
