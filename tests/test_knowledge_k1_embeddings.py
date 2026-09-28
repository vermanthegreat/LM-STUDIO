"""Phase K1: local embeddings and hybrid retrieval (deterministic fake embeddings only)."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from unittest.mock import patch

import db
import httpx
import pytest
from fastapi.testclient import TestClient

from ask_router import answer_question
from config import AppConfig
from knowledge import hybrid
from knowledge import repository as repo
from knowledge.chunking import MAX_CHARS, chunk_text
from knowledge.embedding_index import embedding_status, index_knowledge, sync_chunks
from knowledge.embeddings import (
    EmbeddingError,
    EmbeddingSettings,
    OpenAICompatibleEmbeddingProvider,
    attach_embedding_runtime,
    build_embedding_runtime,
    get_embedding_runtime,
)
from knowledge.ingestion import UploadedFile
from knowledge.retrieval import search_knowledge
from knowledge.schemas import SearchKnowledgeInput
from repositories.sqlite_store import SqliteContactStore
from tests.knowledge_support import make_ingestor

from app import create_app

DEFAULT_LEADS_DB = Path(db.__file__).parent / "leads.db"

CONCEPTS = [
    {"car", "cars", "automobile", "automobiles", "vehicle", "vehicles", "sedan", "fleet"},
    {"invoice", "invoices", "billing", "payment", "payments", "bill"},
    {"login", "authentication", "oauth", "signin", "credentials", "sso"},
    {"cat", "cats", "kitten", "feline"},
    {"latency", "performance", "throughput", "speed", "benchmark"},
]
_WORD = re.compile(r"\w+")


def concept_vector(text: str, dim: int = len(CONCEPTS) + 1) -> list[float]:
    words = [w.lower() for w in _WORD.findall(text)]
    vec = [0.0] * dim
    for w in words:
        for i, group in enumerate(CONCEPTS[: dim - 1]):
            if w in group:
                vec[i] += 1.0
    vec[-1] = 0.05  # small shared component: never a zero vector
    return vec


class FakeProvider:
    """Deterministic local stand-in for an OpenAI-compatible embedding model."""

    def __init__(self, model: str = "fake-embed-v1", *, dim: int = len(CONCEPTS) + 1):
        self.model = model
        self.dim = dim
        self.calls: list[list[str]] = []
        self.fail_substring: str | None = None
        self.fail_code = "embedding_malformed_response"
        self.unavailable_code: str | None = None
        self.zero_substring: str | None = None

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.unavailable_code:
            raise EmbeddingError(self.unavailable_code, "provider down")
        if self.fail_substring and any(self.fail_substring in t for t in texts):
            raise EmbeddingError(self.fail_code, "bad input")
        out = []
        for t in texts:
            if self.zero_substring and self.zero_substring in t:
                out.append([0.0] * self.dim)
            else:
                out.append(concept_vector(t, self.dim))
        return out


def runtime_for(provider, **settings):
    return build_embedding_runtime(EmbeddingSettings(enabled=True, model=provider.model, **settings), provider=provider)


DOCS = {
    "fleet.txt": b"Quarterly automobile report: sedan maintenance costs rose across the fleet.",
    "billing.txt": b"Invoice disputes: payment terms for the enterprise billing account.",
    "sso.md": b"# Access\nOAuth login flow and SSO credentials rotation.",
    "pets.txt": b"The office kitten sleeps near the window.",
}


@pytest.fixture(scope="module", autouse=True)
def default_leads_db_untouched():
    before = DEFAULT_LEADS_DB.stat() if DEFAULT_LEADS_DB.exists() else None
    yield
    after = DEFAULT_LEADS_DB.stat() if DEFAULT_LEADS_DB.exists() else None
    if before is None:
        assert after is None
    else:
        assert after is not None
        assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def corpus(tmp_path, provider):
    ingestor = make_ingestor(tmp_path, embeddings=runtime_for(provider))
    assert ingestor.database_path != DEFAULT_LEADS_DB
    batch = ingestor.ingest_batch([UploadedFile(n, d) for n, d in DOCS.items()])
    ids = {r.filename: r.item_id for r in batch.results}
    return ingestor, ids, batch


def _search(db_path, runtime, query, **kw):
    return search_knowledge(db_path, SearchKnowledgeInput(query=query, **kw), runtime)


def _count(db_path, sql):
    with db.get_conn(db_path) as conn:
        return conn.execute(sql).fetchone()[0]


# --------------------------------------------------------------- provider validation


def _http_provider(handler):
    return OpenAICompatibleEmbeddingProvider(
        base_url="http://127.0.0.1:1234/v1", model="m", timeout=1.0, transport=httpx.MockTransport(handler)
    )


def test_http_provider_parses_and_orders_by_index():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [
            {"index": 1, "embedding": [0.0, 2.0]},
            {"index": 0, "embedding": [1, 0]},
        ]})

    vectors = _http_provider(handler).embed(["a", "b"])
    assert vectors == [[1.0, 0.0], [0.0, 2.0]]
    assert seen["url"] == "http://127.0.0.1:1234/v1/embeddings"
    assert seen["body"] == {"model": "m", "input": ["a", "b"]}


@pytest.mark.parametrize(
    "response,code",
    [
        (httpx.Response(200, json={"nope": []}), "embedding_malformed_response"),
        (httpx.Response(200, text="not json"), "embedding_malformed_response"),
        (httpx.Response(200, json={"data": [{"vector": [1]}]}), "embedding_malformed_response"),
        (httpx.Response(200, json={"data": [{"embedding": []}]}), "embedding_empty_vector"),
        (httpx.Response(200, json={"data": [{"embedding": ["x", 1]}]}), "embedding_non_numeric"),
        (httpx.Response(200, json={"data": [{"embedding": [True, 1.0]}]}), "embedding_non_numeric"),
        (httpx.Response(200, json={"data": [{"embedding": [1.0], "index": 0}, {"embedding": [1.0, 2.0], "index": 1}]}), "embedding_dimension_inconsistent"),
        (httpx.Response(200, json={"data": [{"embedding": [1.0]}]}), "embedding_count_mismatch"),
        (httpx.Response(500, json={"error": "boom"}), "embedding_http_error"),
    ],
)
def test_http_provider_rejects_bad_responses(response, code):
    texts = ["a", "b"] if code in {"embedding_dimension_inconsistent", "embedding_count_mismatch"} else ["a"]
    with pytest.raises(EmbeddingError) as exc:
        _http_provider(lambda _r: response).embed(texts)
    assert exc.value.code == code


def test_http_provider_rejects_nan():
    handler = lambda _r: httpx.Response(200, content=b'{"data": [{"embedding": [NaN, 1.0]}]}')
    with pytest.raises(EmbeddingError) as exc:
        _http_provider(handler).embed(["a"])
    assert exc.value.code == "embedding_non_numeric"


def test_http_provider_timeout_and_unreachable():
    def timeout(_r):
        raise httpx.ReadTimeout("slow")

    def refused(_r):
        raise httpx.ConnectError("refused")

    with pytest.raises(EmbeddingError) as exc:
        _http_provider(timeout).embed(["a"])
    assert exc.value.code == "embedding_timeout"
    with pytest.raises(EmbeddingError) as exc:
        _http_provider(refused).embed(["a"])
    assert exc.value.code == "embedding_unreachable"


def test_runtime_configuration_boundaries():
    assert build_embedding_runtime(EmbeddingSettings()).disabled_reason == "embeddings_disabled"
    assert build_embedding_runtime(EmbeddingSettings(enabled=True)).disabled_reason == "embedding_model_not_configured"
    remote = EmbeddingSettings(enabled=True, model="m", base_url="https://api.example.com/v1")
    assert build_embedding_runtime(remote).disabled_reason == "embedding_remote_endpoint_not_allowed"
    allowed = EmbeddingSettings(enabled=True, model="m", base_url="https://api.example.com/v1", allow_remote=True)
    assert build_embedding_runtime(allowed).enabled
    local = build_embedding_runtime(EmbeddingSettings(enabled=True, model="m", base_url="http://localhost:1234/v1"))
    assert local.enabled and local.model == "m"
    # Stores never pick up embeddings from the environment implicitly.
    assert get_embedding_runtime(SqliteContactStore(Path("unused.db"))).disabled_reason == "embedding_runtime_not_configured"


def test_app_config_defaults_disable_embeddings():
    cfg = AppConfig()
    assert cfg.knowledge_embeddings_enabled is False
    assert cfg.knowledge_embedding_model is None


# --------------------------------------------------------------- chunking


def test_chunking_is_deterministic_and_bounded():
    text = "\n\n".join(f"Paragraph {i} " + "word " * 120 for i in range(12))
    first, second = chunk_text(text), chunk_text(text)
    assert first == second
    assert len(first) > 1
    assert all(len(c.text) <= MAX_CHARS for c in first)
    assert [c.index for c in first] == list(range(len(first)))
    long_para = "x" * 5000
    assert all(len(c.text) <= MAX_CHARS for c in chunk_text(long_para))
    assert chunk_text("") == [] and chunk_text("   \n\n  ") == []


# --------------------------------------------------------------- migration


def test_migration_from_k0_only_database(tmp_path, provider):
    ingestor = make_ingestor(tmp_path)  # K0 path: no embeddings runtime
    item_id = ingestor.ingest_batch([UploadedFile("fleet.txt", DOCS["fleet.txt"])]).results[0].item_id
    db_path = ingestor.database_path
    # Simulate a database created before K1 existed.
    with db.get_conn(db_path) as conn:
        conn.execute("DROP TABLE knowledge_chunk_embeddings")
        conn.execute("DROP TABLE knowledge_chunks")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "knowledge_chunks" not in tables
    before = _search(db_path, None, "sedan maintenance")
    assert [h["id"] for h in before["hits"]] == [item_id]

    SqliteContactStore(db_path).init_db()  # additive migration

    with db.get_conn(db_path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        item = repo.get_item(conn, item_id)
    assert {"knowledge_chunks", "knowledge_chunk_embeddings"} <= tables
    assert item["normalized_text"].startswith("Quarterly automobile report")
    after = _search(db_path, None, "sedan maintenance")
    assert [h["id"] for h in after["hits"]] == [item_id]
    # Re-running the migration is a no-op.
    SqliteContactStore(db_path).init_db()
    report = index_knowledge(db_path, runtime_for(provider))
    assert (report.indexed, report.failed) == (1, 0)


def test_k0_items_searchable_before_embedding(tmp_path, provider):
    ingestor = make_ingestor(tmp_path)
    ingestor.ingest_batch([UploadedFile(n, d) for n, d in DOCS.items()])
    result = _search(ingestor.database_path, runtime_for(provider), "invoice disputes")
    assert result["mode_used"] == "lexical"
    assert result["semantic"]["status"] == "no_index"
    assert result["hits"][0]["original_filename"] == "billing.txt"


# --------------------------------------------------------------- indexing


def test_embedding_created_on_ingest(corpus):
    ingestor, ids, batch = corpus
    for result in batch.results:
        assert result.embedding["status"] == "ok"
        assert result.embedding["indexed"] == result.embedding["chunks"] == 1
    assert _count(ingestor.database_path, "SELECT COUNT(*) FROM knowledge_chunk_embeddings WHERE status='ok'") == 4
    with db.get_conn(ingestor.database_path) as conn:
        status = embedding_status(conn, ingestor.embeddings)
        row = conn.execute("SELECT dimension, length(vector) FROM knowledge_chunk_embeddings LIMIT 1").fetchone()
    assert status["embedded"] == 4 and status["items_fully_embedded"] == 4
    assert status["dimension"] == len(CONCEPTS) + 1
    assert row[1] == 4 * row[0]  # float32 storage


def test_reindex_existing_and_skip_unchanged(tmp_path, provider):
    ingestor = make_ingestor(tmp_path)
    ingestor.ingest_batch([UploadedFile(n, d) for n, d in DOCS.items()])
    runtime = runtime_for(provider)
    first = index_knowledge(ingestor.database_path, runtime)
    assert (first.chunks_total, first.eligible, first.indexed, first.skipped_unchanged) == (4, 4, 4, 0)
    calls_after_first = len(provider.calls)
    second = index_knowledge(ingestor.database_path, runtime)
    assert (second.eligible, second.indexed, second.skipped_unchanged) == (0, 0, 4)
    assert len(provider.calls) == calls_after_first  # nothing re-embedded


def test_reindex_is_bounded_by_limit(tmp_path, provider):
    ingestor = make_ingestor(tmp_path)
    ingestor.ingest_batch([UploadedFile(n, d) for n, d in DOCS.items()])
    report = index_knowledge(ingestor.database_path, runtime_for(provider), limit=3)
    assert (report.eligible, report.indexed, report.deferred) == (4, 3, 1)
    rest = index_knowledge(ingestor.database_path, runtime_for(provider), limit=3)
    assert (rest.indexed, rest.skipped_unchanged) == (1, 3)


def test_changed_content_is_re_embedded_with_stable_chunk_id(corpus, provider):
    ingestor, ids, _ = corpus
    item_id = ids["pets.txt"]
    with db.get_conn(ingestor.database_path) as conn:
        chunk_id = conn.execute("SELECT id FROM knowledge_chunks WHERE item_id = ?", (item_id,)).fetchone()[0]
        conn.execute(
            "UPDATE knowledge_items SET normalized_text = ? WHERE id = ?",
            ("The office cat now sleeps on the invoice pile.", item_id),
        )
        status = embedding_status(conn, ingestor.embeddings, item_id=item_id)
    assert status["embedded"] == 1  # chunk text not yet re-synced
    report = index_knowledge(ingestor.database_path, ingestor.embeddings)
    assert (report.indexed, report.skipped_unchanged) == (1, 3)
    with db.get_conn(ingestor.database_path) as conn:
        row = conn.execute("SELECT id, text FROM knowledge_chunks WHERE item_id = ?", (item_id,)).fetchone()
        n = conn.execute("SELECT COUNT(*) FROM knowledge_chunk_embeddings").fetchone()[0]
    assert row["id"] == chunk_id and "invoice pile" in row["text"]
    assert n == 4  # updated in place, not duplicated


def test_stale_embedding_never_used_for_search(corpus):
    ingestor, ids, _ = corpus
    with db.get_conn(ingestor.database_path) as conn:
        conn.execute(
            "UPDATE knowledge_items SET normalized_text = 'Nothing relevant here.' WHERE id = ?", (ids["fleet.txt"],)
        )
        sync_chunks(conn, ids["fleet.txt"])
        status = embedding_status(conn, ingestor.embeddings, item_id=ids["fleet.txt"])
    assert status["stale"] == 1
    result = _search(ingestor.database_path, ingestor.embeddings, "car", mode="semantic")
    assert ids["fleet.txt"] not in [h["id"] for h in result["hits"]]


def test_model_change_does_not_reuse_old_vectors(corpus):
    ingestor, ids, _ = corpus
    new_provider = FakeProvider(model="fake-embed-v2")
    runtime_v2 = runtime_for(new_provider)
    with db.get_conn(ingestor.database_path) as conn:
        status = embedding_status(conn, runtime_v2)
    assert (status["embedded"], status["missing"], status["other_model_embeddings"]) == (0, 4, 4)
    result = _search(ingestor.database_path, runtime_v2, "car")
    assert result["mode_used"] == "lexical" and result["semantic"]["status"] == "no_index"
    report = index_knowledge(ingestor.database_path, runtime_v2)
    assert report.indexed == 4 and report.model == "fake-embed-v2"
    result = _search(ingestor.database_path, runtime_v2, "car")
    assert result["mode_used"] == "hybrid"
    assert result["hits"][0]["id"] == ids["fleet.txt"]


def test_dimension_mismatch_is_explicit(corpus):
    ingestor, ids, _ = corpus
    wrong_dim = FakeProvider(model="fake-embed-v1", dim=3)
    runtime_bad = runtime_for(wrong_dim)
    item = ingestor.ingest_batch([UploadedFile("new.txt", b"A new sedan for the fleet.")]).results[0]
    with db.get_conn(ingestor.database_path) as conn:
        conn.execute("DELETE FROM knowledge_chunk_embeddings WHERE chunk_id IN "
                     "(SELECT id FROM knowledge_chunks WHERE item_id = ?)", (item.item_id,))
    report = index_knowledge(ingestor.database_path, runtime_bad, item_ids=[item.item_id])
    assert report.failed == 1 and report.error_codes == {"embedding_dimension_mismatch": 1}
    result = _search(ingestor.database_path, runtime_bad, "car")
    assert result["mode_used"] == "lexical"
    assert result["semantic"] == {"status": "error", "error_code": "embedding_dimension_mismatch", "model": "fake-embed-v1"}


def test_malformed_and_zero_vectors_recorded_as_failures(tmp_path, provider):
    provider.fail_substring = "kitten"
    provider.zero_substring = "Invoice"
    ingestor = make_ingestor(tmp_path, embeddings=runtime_for(provider))
    batch = ingestor.ingest_batch([UploadedFile(n, d) for n, d in DOCS.items()])
    by_name = {r.filename: r for r in batch.results}
    assert all(r.status.value == "ingested" for r in batch.results)
    assert by_name["pets.txt"].embedding["error_codes"] == {"embedding_malformed_response": 1}
    assert by_name["billing.txt"].embedding["error_codes"] == {"embedding_zero_vector": 1}
    assert "embedding_failed:embedding_malformed_response" in by_name["pets.txt"].warnings
    with db.get_conn(ingestor.database_path) as conn:
        status = embedding_status(conn, ingestor.embeddings)
    assert (status["embedded"], status["failed"]) == (2, 2)
    # Failed items stay lexically searchable.
    assert _search(ingestor.database_path, ingestor.embeddings, "kitten")["hits"][0]["id"] == by_name["pets.txt"].item_id


def test_provider_timeout_aborts_pass_without_looping(tmp_path, provider):
    ingestor = make_ingestor(tmp_path)
    ingestor.ingest_batch([UploadedFile(n, d) for n, d in DOCS.items()])
    provider.unavailable_code = "embedding_timeout"
    report = index_knowledge(ingestor.database_path, runtime_for(provider, batch_size=2))
    assert report.aborted_code == "embedding_timeout"
    assert len(provider.calls) == 1  # no per-chunk retries, no further batches
    assert (report.failed, report.deferred, report.indexed) == (2, 2, 0)
    assert report.status == "failed"


def test_partial_batch_failure_isolates_bad_chunk(tmp_path, provider):
    ingestor = make_ingestor(tmp_path)
    ingestor.ingest_batch([UploadedFile(n, d) for n, d in DOCS.items()])
    provider.fail_substring = "kitten"
    report = index_knowledge(ingestor.database_path, runtime_for(provider, batch_size=4))
    assert (report.indexed, report.failed, report.status) == (3, 1, "partial")
    assert len(provider.calls) == 1 + 4  # one batch, then one bounded call per chunk


def test_retry_respects_max_attempts_and_explicit_retry(tmp_path, provider):
    ingestor = make_ingestor(tmp_path)
    ingestor.ingest_batch([UploadedFile("pets.txt", DOCS["pets.txt"])])
    provider.fail_substring = "kitten"
    runtime = runtime_for(provider)
    for _ in range(3):
        assert index_knowledge(ingestor.database_path, runtime).failed == 1
    capped = index_knowledge(ingestor.database_path, runtime)
    assert (capped.eligible, capped.skipped_max_attempts, capped.failed) == (0, 1, 0)
    provider.fail_substring = None
    retried = index_knowledge(ingestor.database_path, runtime, retry_failed=True)
    assert (retried.indexed, retried.failed) == (1, 0)
    with db.get_conn(ingestor.database_path) as conn:
        row = conn.execute("SELECT status, error_code, attempts FROM knowledge_chunk_embeddings").fetchone()
    assert (row["status"], row["error_code"], row["attempts"]) == ("ok", None, 4)  # 3 failures + 1 success; capped pass made no attempt


def test_duplicate_ingestion_creates_no_duplicate_embeddings(corpus):
    ingestor, ids, _ = corpus
    again = ingestor.ingest_batch([UploadedFile("fleet-copy.txt", DOCS["fleet.txt"])]).results[0]
    assert again.status.value == "duplicate" and again.embedding is None
    index_knowledge(ingestor.database_path, ingestor.embeddings)
    assert _count(ingestor.database_path, "SELECT COUNT(*) FROM knowledge_chunks") == 4
    assert _count(ingestor.database_path, "SELECT COUNT(*) FROM knowledge_chunk_embeddings") == 4


def test_empty_knowledge_base(tmp_path, provider):
    ingestor = make_ingestor(tmp_path)
    runtime = runtime_for(provider)
    report = index_knowledge(ingestor.database_path, runtime)
    assert (report.chunks_total, report.indexed, report.status) == (0, 0, "ok")
    for mode in ("auto", "hybrid", "semantic", "lexical"):
        result = _search(ingestor.database_path, runtime, "anything", mode=mode)
        assert result["count"] == 0 and result["hits"] == []
    with db.get_conn(ingestor.database_path) as conn:
        status = embedding_status(conn, runtime)
    assert status["items_total"] == 0 and status["chunks_total"] == 0
    assert provider.calls == []  # no index: the query is never embedded


def test_disabled_runtime_index_is_noop(tmp_path):
    ingestor = make_ingestor(tmp_path)
    ingestor.ingest_batch([UploadedFile("a.txt", b"text")])
    report = index_knowledge(ingestor.database_path, build_embedding_runtime(EmbeddingSettings()))
    assert report.status == "disabled" and report.disabled_reason == "embeddings_disabled"
    assert _count(ingestor.database_path, "SELECT COUNT(*) FROM knowledge_chunks") == 0


# --------------------------------------------------------------- retrieval


def test_semantic_retrieval_without_keyword_overlap(corpus):
    ingestor, ids, _ = corpus
    runtime = ingestor.embeddings
    lexical = _search(ingestor.database_path, runtime, "car", mode="lexical")
    assert lexical["count"] == 0  # "car" appears in no document
    semantic = _search(ingestor.database_path, runtime, "car", mode="semantic")
    assert semantic["mode_used"] == "semantic"
    assert [h["id"] for h in semantic["hits"]] == [ids["fleet.txt"]]
    hybrid_result = _search(ingestor.database_path, runtime, "car")
    assert hybrid_result["mode_requested"] == "auto" and hybrid_result["mode_used"] == "hybrid"
    top = hybrid_result["hits"][0]
    assert top["id"] == ids["fleet.txt"]
    assert top["retrieval"]["evidence"] == ["semantic"]
    assert "automobile" in top["snippet"]


def test_lexical_without_embeddings_matches_k0(corpus):
    ingestor, ids, _ = corpus
    disabled = build_embedding_runtime(EmbeddingSettings())
    k1 = _search(ingestor.database_path, disabled, "payment terms")
    with db.get_conn(ingestor.database_path) as conn:
        k0 = repo.search_items(conn, query="payment terms", filters=repo.KnowledgeSearchFilters(), limit=10)
    assert k1["mode_used"] == "lexical" and k1["semantic"]["status"] == "disabled"
    assert [(h["id"], h["snippet"], h["ranking"]) for h in k1["hits"]] == [
        (h["id"], h["snippet"], h["ranking"]) for h in k0["hits"]
    ]
    explicit = _search(ingestor.database_path, disabled, "payment terms", mode="hybrid")
    assert explicit["mode_used"] == "lexical"
    assert explicit["semantic"] == {"status": "disabled", "reason": "embeddings_disabled", "model": None}


def test_hybrid_deduplicates_and_is_deterministic(corpus):
    ingestor, ids, _ = corpus
    runtime = ingestor.embeddings
    first = _search(ingestor.database_path, runtime, "OAuth login")
    item_ids = [h["id"] for h in first["hits"]]
    assert len(item_ids) == len(set(item_ids))
    top = first["hits"][0]
    assert top["id"] == ids["sso.md"]
    assert top["retrieval"]["evidence"] == ["lexical", "semantic"]
    assert (top["retrieval"]["lexical_rank"], top["retrieval"]["semantic_rank"]) == (1, 1)
    assert top["retrieval"]["rrf_score"] == round(2 / 61, 10)
    for _ in range(3):
        again = _search(ingestor.database_path, runtime, "OAuth login")
        assert json.dumps(again, sort_keys=True) == json.dumps(first, sort_keys=True)


def test_rrf_tie_breaking(corpus):
    ingestor, ids, _ = corpus
    a, b, c = sorted([ids["fleet.txt"], ids["billing.txt"], ids["pets.txt"]])
    with db.get_conn(ingestor.database_path) as conn:
        lexical = [repo.get_item(conn, b, include_text=False), repo.get_item(conn, c, include_text=False)]
        for rank, hit in enumerate(lexical, start=1):
            hit["source"] = {"knowledge_item_id": hit["id"]}
            hit["snippet"] = "lex"
            hit["ranking"] = {"position": rank}

        def sem(item_id):
            chunk = conn.execute("SELECT id, chunk_index, char_start, char_end, text FROM knowledge_chunks WHERE item_id = ?", (item_id,)).fetchone()
            return {"item_id": item_id, "chunk_id": chunk[0], "chunk_index": chunk[1], "char_start": chunk[2],
                    "char_end": chunk[3], "text": chunk[4], "score": 0.9}

        # b: lexical rank 1; a: semantic rank 1 -> equal RRF and equal best rank -> lower id first.
        fused = hybrid.fuse(conn, lexical_hits=lexical[:1], semantic_hits=[sem(a)], limit=10)
        assert [h["id"] for h in fused] == [a, b]
        # c: lexical rank 2 + semantic rank 2 beats a single rank-1 list.
        fused = hybrid.fuse(conn, lexical_hits=lexical, semantic_hits=[sem(a), sem(c)], limit=10)
        assert [h["id"] for h in fused] == [c, a, b]
        assert [h["ranking"]["position"] for h in fused] == [1, 2, 3]
        assert fused[0]["retrieval"]["rrf_score"] == round(2 / 62, 10)


def test_citations_are_stable_and_source_bound(corpus):
    ingestor, ids, _ = corpus
    runtime = ingestor.embeddings
    results = [_search(ingestor.database_path, runtime, "vehicle billing kitten") for _ in range(3)]
    cites = [[(h["id"], h["source"].get("chunk_id")) for h in r["hits"]] for r in results]
    assert cites[0] == cites[1] == cites[2]
    with db.get_conn(ingestor.database_path) as conn:
        for hit in results[0]["hits"]:
            assert hit["source"]["knowledge_item_id"] == hit["id"]
            assert conn.execute("SELECT 1 FROM knowledge_items WHERE id = ?", (hit["id"],)).fetchone()
            chunk = hit["retrieval"].get("chunk")
            if chunk:
                row = conn.execute("SELECT item_id, text FROM knowledge_chunks WHERE id = ?", (chunk["chunk_id"],)).fetchone()
                assert row["item_id"] == hit["id"]
                assert row["text"] == chunk["text"]


def test_deleted_rows_are_never_returned(corpus):
    ingestor, ids, _ = corpus
    with db.get_conn(ingestor.database_path) as conn:
        conn.execute("DELETE FROM knowledge_fts WHERE rowid = ?", (ids["fleet.txt"],))
        conn.execute("DELETE FROM knowledge_items WHERE id = ?", (ids["fleet.txt"],))
        assert conn.execute("SELECT COUNT(*) FROM knowledge_chunks WHERE item_id = ?", (ids["fleet.txt"],)).fetchone()[0] == 0
    for mode in ("semantic", "hybrid"):
        result = _search(ingestor.database_path, ingestor.embeddings, "car sedan", mode=mode)
        assert ids["fleet.txt"] not in [h["id"] for h in result["hits"]]


def test_query_time_provider_failure_falls_back_to_lexical(corpus, provider):
    ingestor, ids, _ = corpus
    provider.unavailable_code = "embedding_timeout"
    calls_before = len(provider.calls)
    with patch("knowledge.retrieval.logger") as log:
        result = _search(ingestor.database_path, ingestor.embeddings, "invoice disputes")
    assert result["mode_requested"] == "auto" and result["mode_used"] == "lexical"
    assert result["semantic"] == {"status": "error", "error_code": "embedding_timeout", "model": "fake-embed-v1"}
    assert result["hits"][0]["id"] == ids["billing.txt"]
    assert len(provider.calls) == calls_before + 1  # exactly one attempt per request
    log.warning.assert_called_once()
    semantic_only = _search(ingestor.database_path, ingestor.embeddings, "car", mode="semantic")
    assert semantic_only["mode_used"] == "lexical" and semantic_only["count"] == 0  # nothing fabricated


def test_min_score_and_bounded_results(corpus):
    ingestor, ids, _ = corpus
    # "car kitten" is ~0.71 cosine to the fleet and pets documents.
    loose = _search(ingestor.database_path, ingestor.embeddings, "car kitten", mode="semantic")
    assert {h["id"] for h in loose["hits"]} == {ids["fleet.txt"], ids["pets.txt"]}
    strict = runtime_for(ingestor.embeddings.provider, min_score=0.9)
    result = _search(ingestor.database_path, strict, "car kitten", mode="semantic")
    assert result["count"] == 0  # below threshold: no weak semantic filler
    capped = runtime_for(ingestor.embeddings.provider, max_candidates=2)
    result = _search(ingestor.database_path, capped, "car kitten invoice login", mode="semantic")
    assert result["semantic"]["candidates"] == 2 and result["semantic"]["truncated"] is True
    limited = _search(ingestor.database_path, ingestor.embeddings, "car kitten invoice login", limit=2)
    assert limited["count"] == 2


def test_filters_apply_to_semantic_results(corpus):
    ingestor, ids, _ = corpus
    result = _search(ingestor.database_path, ingestor.embeddings, "car", mode="semantic", content_kind="image")
    assert result["count"] == 0


def test_long_document_cites_matching_chunk(tmp_path, provider):
    ingestor = make_ingestor(tmp_path, embeddings=runtime_for(provider))
    filler = "\n\n".join("General company history paragraph " + "lorem " * 150 for _ in range(4))
    text = filler + "\n\nThe sedan fleet needs new tyres."
    item_id = ingestor.ingest_batch([UploadedFile("long.txt", text.encode())]).results[0].item_id
    result = _search(ingestor.database_path, ingestor.embeddings, "vehicle", mode="semantic")
    hit = result["hits"][0]
    assert hit["id"] == item_id
    assert hit["retrieval"]["chunk"]["chunk_index"] > 0
    assert "sedan fleet" in hit["retrieval"]["chunk"]["text"]


# --------------------------------------------------------------- /ask and HTTP


def test_ask_uses_only_retrieved_chunks_as_evidence(corpus):
    ingestor, ids, _ = corpus
    store = SqliteContactStore(ingestor.database_path)
    attach_embedding_runtime(store, ingestor.embeddings)
    fleet = ids["fleet.txt"]
    seen = {}

    def fake_chat(messages, **_k):
        seen["payload"] = json.loads(messages[-1]["content"])
        return f"Sedan maintenance costs rose [K{fleet}]."

    with patch("llm.chat_completion", side_effect=fake_chat):
        result = answer_question("knowledge: car", use_llm=True, store=store)
    assert result["data"]["grounded_answer"] is True
    evidence = seen["payload"]["evidence"]
    returned = {r["id"] for r in result["data"]["knowledge"]}
    assert {int(p["marker"][1:]) for p in evidence} <= returned
    assert evidence[0]["marker"] == f"K{fleet}"
    assert evidence[0]["text"] == DOCS["fleet.txt"].decode()
    record = result["data"]["knowledge"][0]
    assert record["retrieval"]["evidence"] == ["semantic"]
    assert "text" not in record["retrieval"]["chunk"]
    with db.get_conn(ingestor.database_path) as conn:
        row = conn.execute("SELECT status, tool_name FROM command_log WHERE id = ?", (result["data"]["command_id"],)).fetchone()
    assert (row["status"], row["tool_name"]) == ("succeeded", "search_knowledge")
    with db.get_conn(ingestor.database_path) as conn:
        logged = conn.execute("SELECT result_summary_json FROM command_log WHERE id = ?", (result["data"]["command_id"],)).fetchone()[0]
    logged_records = json.loads(logged)["records"]
    # K1 adds chunk references to the audit log, never chunk text (K0's bounded snippet is unchanged).
    assert logged_records[0]["retrieval"]["chunk"]["chunk_id"] == record["retrieval"]["chunk"]["chunk_id"]
    assert all("text" not in (r.get("retrieval") or {}).get("chunk", {}) for r in logged_records)


def test_ask_without_runtime_stays_lexical(corpus):
    ingestor, ids, _ = corpus
    store = SqliteContactStore(ingestor.database_path)  # no runtime attached
    result = answer_question("knowledge: car", store=store)
    assert result["intent"] == "knowledge_search"
    assert result["data"]["record_count"] == 0


@pytest.fixture
def client(tmp_path):
    cfg = AppConfig(
        database_path=tmp_path / "app.db",
        knowledge_storage_dir=tmp_path / "store",
        knowledge_classify=False,
        port=8025,
    )
    with TestClient(create_app(cfg), base_url="http://127.0.0.1:8025") as test_client:
        yield test_client


def _upload(client, name, data):
    return client.post("/knowledge/ingest", files=[("files", (name, data, "text/plain"))])


def test_http_default_config_is_lexical_and_reindex_refused(client):
    _upload(client, "fleet.txt", DOCS["fleet.txt"])
    body = client.get("/api/knowledge/search", params={"q": "sedan"}).json()
    assert body["mode_used"] == "lexical" and body["semantic"]["status"] == "disabled"
    status = client.get("/api/knowledge/embeddings/status").json()
    assert status["runtime"]["enabled"] is False and status["runtime"]["disabled_reason"] == "embeddings_disabled"
    refused = client.post("/knowledge/embeddings/reindex", json={})
    assert refused.status_code == 409 and refused.json()["error_code"] == "embeddings_disabled"
    assert "Embeddings disabled" in client.get("/knowledge").text


def test_http_reindex_status_and_hybrid_search(client):
    for name, data in DOCS.items():
        _upload(client, name, data)
    provider = FakeProvider()
    attach_embedding_runtime(client.app.state.store, runtime_for(provider))
    before = client.get("/api/knowledge/embeddings/status").json()
    assert before["embedded"] == 0 and before["items_with_text"] == 4
    blocked = client.post("/knowledge/embeddings/reindex", json={}, headers={"Origin": "https://evil.example"})
    assert blocked.status_code == 403
    bad = client.post("/knowledge/embeddings/reindex", json={"limit": 0})
    assert bad.status_code == 422 and bad.json()["error_code"] == "invalid_reindex_request"
    unknown = client.post("/knowledge/embeddings/reindex", json={"sql": "drop"})
    assert unknown.status_code == 422
    response = client.post("/knowledge/embeddings/reindex", json={"limit": 10})
    assert response.status_code == 200
    body = response.json()
    assert body["report"]["indexed"] == 4 and body["status"]["embedded"] == 4
    assert body["command_id"]
    item_status = client.get("/api/knowledge/embeddings/status", params={"item_id": 1}).json()
    assert item_status["chunks_total"] == 1 and item_status["embedded"] == 1
    search = client.get("/api/knowledge/search", params={"q": "car", "mode": "hybrid"}).json()
    assert search["mode_used"] == "hybrid"
    assert search["hits"][0]["original_filename"] == "fleet.txt"
    assert client.get("/api/knowledge/search", params={"q": "car", "mode": "vector"}).status_code == 422
    page = client.get("/knowledge").text
    assert "fake-embed-v1" in page and "4 / 4 chunks embedded" in page


def test_default_suite_never_contacts_live_service(client):
    guard = AssertionError("embedding endpoint contacted")
    with patch("knowledge.embeddings.OpenAICompatibleEmbeddingProvider.embed", side_effect=guard):
        _upload(client, "fleet.txt", DOCS["fleet.txt"])
        client.get("/api/knowledge/search", params={"q": "sedan", "mode": "hybrid"})
