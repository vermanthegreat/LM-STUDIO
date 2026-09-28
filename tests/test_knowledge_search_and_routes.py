"""Knowledge retrieval, filters, HTTP routes, and /ask integration."""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import db
import pytest
from fastapi.testclient import TestClient

from ask_router import answer_question
from config import AppConfig
from knowledge import repository as repo
from knowledge.ingestion import KnowledgeRuntimeUnsupportedError, UploadedFile, require_knowledge_sqlite_runtime
from knowledge.retrieval import parse_question
from knowledge.schemas import KnowledgeSearchFilters
from knowledge.tool import handle_search_knowledge
from knowledge.schemas import SearchKnowledgeInput
from repositories.sqlite_store import SqliteContactStore
from tests.knowledge_support import PNG_1X1, classification_json, make_docx, make_ingestor, make_pdf

from app import create_app

DOCS = {
    "claude_findings.md": (
        b"# Claude findings\nClaude reviewed the ChatGPT plugin manifest and found OAuth scope issues.",
        dict(
            summary="Claude's review of the ChatGPT plugin found OAuth scope issues.",
            project="CommerceGov",
            category="research",
            topics=["ChatGPT plugin", "OAuth"],
            entities=[{"name": "Claude", "type": "product"}],
            event_date="2026-07-10",
        ),
    ),
    "benchmark.txt": (
        b"Benchmark run: indexing 5000 products took 41 seconds on the local box.",
        dict(
            summary="Benchmark of indexing 5000 products.",
            project="CatalogSync",
            category="benchmark",
            topics=["performance"],
            event_date="2026-09-03",
        ),
    ),
    "call_notes.txt": (
        b"Call with Nikolay Galinov about CommerceGov OAuth rollout and next steps.",
        dict(
            summary="Call notes with Nikolay Galinov on OAuth rollout.",
            project="CommerceGov",
            category="meeting-notes",
            topics=["OAuth"],
            entities=[{"name": "Nikolay Galinov", "type": "person"}],
        ),
    ),
}


def _chat_for_docs(messages, **_kwargs):
    user = messages[-1]["content"]
    for name, (_data, meta) in DOCS.items():
        if name in user:
            return classification_json(**meta)
    return None


@pytest.fixture
def corpus(tmp_path):
    ingestor = make_ingestor(tmp_path, chat_fn=_chat_for_docs)
    lead, _ = db.upsert_lead({"company_name": "Galinov Co"}, db_path=ingestor.database_path)
    person = db.add_person(lead["id"], {"name": "Nikolay Galinov"}, db_path=ingestor.database_path)
    batch = ingestor.ingest_batch([UploadedFile(name, data) for name, (data, _m) in DOCS.items()])
    ids = {r.filename: r.item_id for r in batch.results}
    return SimpleNamespace(ingestor=ingestor, db_path=ingestor.database_path, ids=ids, person_id=person["id"])


def _search(db_path, query="", **filters):
    with db.get_conn(db_path) as conn:
        return repo.search_items(conn, query=query, filters=KnowledgeSearchFilters(**filters), limit=10)


@pytest.mark.parametrize(
    "query,expected",
    [
        ("Claude findings ChatGPT plugin", "claude_findings.md"),
        ("benchmark 5000 products", "benchmark.txt"),
        ("Nikolay Galinov", "call_notes.txt"),
    ],
)
def test_fts_example_queries_rank_expected_item_first(corpus, query, expected):
    result = _search(corpus.db_path, query)
    assert result["engine"] == "fts5_bm25"
    assert result["hits"][0]["id"] == corpus.ids[expected]
    hit = result["hits"][0]
    assert hit["snippet"]
    assert hit["source"]["original_filename"] == expected
    assert hit["source"]["original_url"] == f"/knowledge/items/{hit['id']}/original"
    assert hit["ranking"]["position"] == 1


def test_oauth_commercegov_matches_both_commercegov_oauth_items(corpus):
    result = _search(corpus.db_path, "OAuth CommerceGov")
    ids = {h["id"] for h in result["hits"]}
    assert {corpus.ids["claude_findings.md"], corpus.ids["call_notes.txt"]} <= ids
    assert corpus.ids["benchmark.txt"] not in ids


def test_any_term_fallback_when_all_terms_do_not_match(corpus):
    result = _search(corpus.db_path, "benchmark zebra")
    assert result["match_mode"] == "any_term"
    assert result["hits"][0]["id"] == corpus.ids["benchmark.txt"]


def test_fts_query_is_injection_safe(corpus):
    for hostile in ['" OR 1=1 --', "NEAR(a b)", "col:value *", "'; DROP TABLE knowledge_items; --"]:
        _search(corpus.db_path, hostile)
    with db.get_conn(corpus.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM knowledge_items").fetchone()[0] == 3


def test_metadata_filters(corpus):
    ids = corpus.ids

    def hit_ids(**kw):
        return {h["id"] for h in _search(corpus.db_path, **kw)["hits"]}

    assert hit_ids(project="commercegov") == {ids["claude_findings.md"], ids["call_notes.txt"]}
    assert hit_ids(category="benchmark") == {ids["benchmark.txt"]}
    assert hit_ids(topic="oauth") == {ids["claude_findings.md"], ids["call_notes.txt"]}
    assert hit_ids(entity="nikolay galinov") == {ids["call_notes.txt"]}
    assert hit_ids(person_id=corpus.person_id) == {ids["call_notes.txt"]}
    assert hit_ids(content_kind="text") == set(ids.values())
    assert hit_ids(content_kind="image") == set()
    assert hit_ids(event_from=date(2026, 9, 1)) == {ids["benchmark.txt"]}
    assert hit_ids(event_to=date(2026, 7, 31)) == {ids["claude_findings.md"]}
    assert hit_ids(query="OAuth", project="CommerceGov", topic="ChatGPT plugin") == {ids["claude_findings.md"]}
    today = date.today()
    assert hit_ids(captured_from=today, captured_to=today) == set(ids.values())


def test_effective_date_uses_event_date_then_capture_date(corpus):
    ids = corpus.ids
    hits = {h["id"] for h in _search(corpus.db_path, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))["hits"]}
    # benchmark has event_date in September; call_notes has no event_date so capture date applies.
    assert ids["benchmark.txt"] in hits
    assert ids["claude_findings.md"] not in hits


def test_related_items_by_shared_topics(corpus):
    with db.get_conn(corpus.db_path) as conn:
        related = repo.related_items(conn, corpus.ids["claude_findings.md"])
    assert [r["id"] for r in related] == [corpus.ids["call_notes.txt"]]


def test_facets_are_metadata_views(corpus):
    with db.get_conn(corpus.db_path) as conn:
        facets = repo.facet_counts(conn)
    assert {"value": "CommerceGov", "count": 2} in facets["projects"]
    assert any(f["value"] == "2026-09" for f in facets["months"])


def test_parse_question_extracts_terms_and_explicit_dates():
    query, filters = parse_question("What do my notes say about OAuth in September 2026?")
    assert query == "OAuth"
    assert filters == {"date_from": "2026-09-01", "date_to": "2026-09-30"}
    query, filters = parse_question("benchmark results since 2026-08-15")
    assert query == "benchmark results"
    assert filters == {"date_from": "2026-08-15"}
    assert parse_question("what happened next week")[1] == {}


# --------------------------------------------------------------------- tool + runtime


def test_search_knowledge_tool_envelope(corpus):
    store = SqliteContactStore(corpus.db_path)
    result = handle_search_knowledge(store, SearchKnowledgeInput(query="benchmark 5000 products"))
    assert result.status == "ok"
    assert result.records[0]["id"] == corpus.ids["benchmark.txt"]
    assert f"knowledge_item:{corpus.ids['benchmark.txt']}" in result.provenance
    empty = handle_search_knowledge(store, SearchKnowledgeInput(query="nonexistentterm"))
    assert empty.record_count == 0
    assert "no_matches_absence_is_not_evidence" in empty.warnings


def test_search_knowledge_input_rejects_unknown_fields():
    with pytest.raises(Exception):
        SearchKnowledgeInput.model_validate({"query": "x", "sql": "DROP TABLE"})


def test_postgres_runtime_fails_closed():
    pg_store = SimpleNamespace(backend="postgresql")
    with pytest.raises(KnowledgeRuntimeUnsupportedError):
        require_knowledge_sqlite_runtime(pg_store)
    result = handle_search_knowledge(pg_store, SearchKnowledgeInput(query="x"))
    assert result.status == "error"
    assert "knowledge_postgresql_runtime_unsupported" in result.warnings


# --------------------------------------------------------------------- HTTP


@pytest.fixture
def client(tmp_path):
    cfg = AppConfig(
        database_path=tmp_path / "app.db",
        knowledge_storage_dir=tmp_path / "store",
        knowledge_classify=False,
        knowledge_max_upload_bytes=1024 * 1024,
        port=8025,
    )
    with TestClient(create_app(cfg), base_url="http://127.0.0.1:8025") as test_client:
        test_client.tmp_path = tmp_path
        yield test_client


def _upload(client, *files, project="", headers=None):
    return client.post(
        "/knowledge/ingest",
        files=[("files", f) for f in files],
        data={"project": project},
        headers=headers or {},
    )


def test_ingest_route_multi_file_statuses(client):
    response = _upload(
        client,
        ("notes.md", b"# OAuth\nCommerceGov OAuth notes", "text/markdown"),
        ("report.pdf", make_pdf(["benchmark 5000 products"]), "application/pdf"),
        ("review.docx", make_docx(heading="Review", paragraphs=["Docx body"]), "application/octet-stream"),
        ("shot.png", PNG_1X1, "image/png"),
        ("../../evil.exe", b"MZ\x00\x00binary", "application/octet-stream"),
        project="CommerceGov",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    assert body["command_id"]
    by_name = {r["filename"]: r for r in body["results"]}
    assert set(by_name) == {"notes.md", "report.pdf", "review.docx", "shot.png", "evil.exe"}
    assert by_name["evil.exe"]["status"] == "unsupported"
    assert by_name["shot.png"]["vision_status"] == "unavailable"
    assert by_name["notes.md"]["classification_status"] == "disabled"
    assert by_name["notes.md"]["preview"]["project"] == "CommerceGov"
    stored = [p for p in (client.tmp_path / "store").rglob("*") if p.is_file()]
    assert len(stored) == 4
    assert all((client.tmp_path / "store") in p.parents for p in stored)
    assert not any("evil" in p.name for p in stored)


def test_ingest_route_duplicate(client):
    _upload(client, ("a.txt", b"same", "text/plain"))
    body = _upload(client, ("b.txt", b"same", "text/plain")).json()
    assert body["results"][0]["status"] == "duplicate"
    assert body["results"][0]["existing_item_id"] == 1


def test_ingest_route_all_failed_returns_422(client):
    response = _upload(client, ("tool.exe", b"MZ\x00\x00binary", "application/octet-stream"))
    assert response.status_code == 422
    assert response.json()["status"] == "error"


def test_ingest_route_rejects_oversize(client):
    response = _upload(client, ("big.txt", b"x" * (1024 * 1024 + 5), "text/plain"))
    assert response.json()["results"][0]["error_code"] == "file_too_large"


def test_ingest_route_blocks_cross_origin(client):
    response = _upload(
        client, ("a.txt", b"x", "text/plain"), headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403
    assert response.json()["error_code"] == "unsafe_origin"
    assert client.get("/api/knowledge/items").json()["total"] == 0


def test_item_routes_and_original_download(client):
    _upload(client, ("notes.md", b"# OAuth\nCommerceGov <script>alert(1)</script>", "text/markdown"))
    listing = client.get("/api/knowledge/items").json()
    assert listing["total"] == 1
    item = client.get("/api/knowledge/items/1").json()
    assert item["normalized_text"].startswith("# OAuth")
    assert item["related"] == []
    page = client.get("/knowledge/items/1")
    assert page.status_code == 200
    assert "<script>alert(1)</script>" not in page.text
    original = client.get("/knowledge/items/1/original")
    assert original.status_code == 200
    assert original.content.startswith(b"# OAuth")
    assert original.headers["content-type"] == "application/octet-stream"
    assert "attachment" in original.headers["content-disposition"]
    assert original.headers["x-content-type-options"] == "nosniff"
    assert client.get("/knowledge/items/999").status_code == 404
    assert client.get("/api/knowledge/items/999").status_code == 404
    assert client.get("/knowledge/items/999/original").status_code == 404


def test_knowledge_page_renders(client):
    _upload(client, ("notes.md", b"# OAuth\nCommerceGov", "text/markdown"), project="CommerceGov")
    page = client.get("/knowledge")
    assert page.status_code == 200
    assert 'id="dropzone"' in page.text
    assert "notes.md" in page.text
    assert "Image vision disabled" in page.text


def test_search_route_and_validation(client):
    _upload(client, ("notes.md", b"CommerceGov OAuth rollout", "text/markdown"), project="CommerceGov")
    body = client.get("/api/knowledge/search", params={"q": "OAuth", "project": "CommerceGov"}).json()
    assert body["count"] == 1
    assert body["hits"][0]["snippet"]
    bad = client.get("/api/knowledge/search", params={"q": "x", "event_from": "yesterday"})
    assert bad.status_code == 422
    assert bad.json()["error_code"] == "invalid_knowledge_search"
    unknown = client.get("/api/knowledge/search", params={"q": "x", "sql": "1"})
    assert unknown.status_code == 422


# --------------------------------------------------------------------- /ask


def test_ask_routes_knowledge_question_with_audit(corpus):
    store = SqliteContactStore(corpus.db_path)
    result = answer_question("knowledge: benchmark 5000 products", store=store)
    assert result["intent"] == "knowledge_search"
    assert f"[K{corpus.ids['benchmark.txt']}] benchmark.txt" in result["answer"]
    assert result["data"]["grounded_answer"] is False
    with db.get_conn(corpus.db_path) as conn:
        row = conn.execute(
            "SELECT status, tool_name FROM command_log WHERE id = ?", (result["data"]["command_id"],)
        ).fetchone()
    assert (row["status"], row["tool_name"]) == ("succeeded", "search_knowledge")


def test_ask_phrase_trigger_and_no_match_is_not_negative_evidence(corpus):
    store = SqliteContactStore(corpus.db_path)
    result = answer_question("What do my notes say about quantum teleportation?", store=store)
    assert result["intent"] == "knowledge_search"
    assert "not evidence" in result["answer"]


def test_ask_grounded_answer_uses_only_retrieved_evidence(corpus):
    store = SqliteContactStore(corpus.db_path)
    bench_id = corpus.ids["benchmark.txt"]
    seen = {}

    def fake_chat(messages, **_kwargs):
        seen["payload"] = json.loads(messages[-1]["content"])
        return f"Indexing 5000 products took 41 seconds [K{bench_id}]."

    with patch("llm.chat_completion", side_effect=fake_chat):
        result = answer_question("knowledge: benchmark 5000 products", use_llm=True, store=store)
    assert result["data"]["grounded_answer"] is True
    assert result["answer"].startswith("Answer (local model, restricted to the retrieved evidence below)")
    assert f"[K{bench_id}] benchmark.txt" in result["answer"]
    markers = [p["marker"] for p in seen["payload"]["evidence"]]
    assert markers[0] == f"K{bench_id}"
    assert "41 seconds" in seen["payload"]["evidence"][0]["text"]


def test_ask_rejects_uncited_or_fabricated_citations(corpus):
    store = SqliteContactStore(corpus.db_path)
    for reply in ("It took 41 seconds.", "It took 41 seconds [K999]."):
        with patch("llm.chat_completion", return_value=reply):
            result = answer_question("knowledge: benchmark 5000 products", use_llm=True, store=store)
        assert result["data"]["grounded_answer"] is False
        assert not result["answer"].startswith("Answer (local model")


def test_existing_ask_intents_unchanged(corpus):
    store = SqliteContactStore(corpus.db_path)
    assert answer_question("how many companies do we have?", store=store)["intent"] == "count_leads"
    assert answer_question("show top leads", store=store)["intent"] == "top_leads"


def test_ask_json_route_knowledge(client):
    _upload(client, ("notes.md", b"CommerceGov OAuth rollout plan", "text/markdown"))
    response = client.post("/ask", json={"question": "search knowledge OAuth rollout"})
    assert response.status_code == 200
    body = response.json()
    assert body["intent"] == "knowledge_search"
    assert "[K1] notes.md" in body["answer"]
