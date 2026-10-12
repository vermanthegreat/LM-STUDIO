"""Community capture parser, persistence and route tests.

Fixtures are SYNTHETIC (tests/fixtures/community/README.md); they prove the
implementation against the Discourse topic JSON shape, not live Shopify.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import community_db
import pytest
from community_parser import parse_capture, parse_topic_url
from config import AppConfig
from errors import ValidationError
from fastapi.testclient import TestClient
from repositories.sqlite_store import SqliteContactStore
from services.community_ingest_service import ingest_capture

from app import create_app

FIX = Path(__file__).parent / "fixtures" / "community"
URL = "https://forum.example.invalid/t/synthetic-checkout-extension-question/90001"
TEXT_URL = "https://forum.example.invalid/t/synthetic-rendered/90002"


def _read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


@pytest.fixture
def store(tmp_path):
    s = SqliteContactStore(tmp_path / "community.db")
    s.init_db()
    return s


def _count(store, table: str) -> int:
    with sqlite3.connect(store.database_path) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _crm_snapshot(store) -> dict:
    with sqlite3.connect(store.database_path) as conn:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("leads", "people", "interactions", "tasks", "raw_sources")}


# ------------------------------------------------------------------ parser


def test_parse_topic_url_variants():
    assert parse_topic_url("https://H.example/t/slug/12") == ("h.example", "slug", 12, None)
    assert parse_topic_url("https://h.example/t/slug/12/5") == ("h.example", "slug", 12, 5)
    assert parse_topic_url("https://h.example/t/12") == ("h.example", None, 12, None)
    for bad in ("ftp://h/t/s/1", "https://h/x/s/1", "https://h/t/1/2", "not a url", ""):
        with pytest.raises(ValidationError):
            parse_topic_url(bad)


def test_json_extraction_matches_fixture():
    thread = parse_capture(_read("synthetic_topic_v1.json"), source_url=URL)
    t = thread.topic
    assert (t.forum_host, t.topic_id, t.slug) == ("forum.example.invalid", 90001, "synthetic-checkout-extension-question")
    assert t.title == "SYNTHETIC: Checkout extension question"
    assert t.category == "7" and t.author_username == "alice_dev"
    assert t.created_at == "2025-01-10T09:00:00+00:00" and t.posts_count_declared == 4
    assert thread.errors == []
    got = [(p.post_number, p.post_id, p.username, p.reply_to_post_number) for p in thread.posts]
    assert got == [(1, 501, "alice_dev", None), (2, 502, "bob_partner", 1), (3, 503, "alice_dev", 2), (4, 504, "carol_agency", 1)]
    assert thread.posts[0].body == "<p>How do I customise the checkout?</p>" and thread.posts[0].body_format == "html"
    assert thread.posts[1].created_at == "2025-01-10T10:30:00+00:00"


def test_json_chronological_order_independent_of_input_order():
    data = json.loads(_read("synthetic_topic_v1.json"))
    data["post_stream"]["posts"].reverse()
    thread = parse_capture(json.dumps(data), source_url=URL)
    assert [p.post_number for p in thread.posts] == [1, 2, 3, 4]


def test_missing_fields_stay_unknown():
    payload = {"post_stream": {"posts": [{"post_number": 1, "username": "x", "cooked": "<p>hi</p>"}]}}
    thread = parse_capture(json.dumps(payload), source_url=URL)
    p = thread.posts[0]
    assert (p.post_id, p.created_at, p.updated_at, p.reply_to_post_number) == (None, None, None, None)
    assert (thread.topic.title, thread.topic.category, thread.topic.author_username) == (None, None, None)


def test_ambiguous_identities_rejected():
    data = json.loads(_read("synthetic_topic_v1.json"))
    posts = data["post_stream"]["posts"]
    dup = dict(posts[1], cooked="<p>different</p>")
    posts.append(dup)  # same post_number 2, different body
    posts[3]["id"] = 501  # post id reused by #1 and #4
    thread = parse_capture(json.dumps(data), source_url=URL)
    codes = {e.code for e in thread.errors}
    assert {"duplicate_post_number", "duplicate_post_id"} <= codes
    assert [p.post_number for p in thread.posts] == [3]  # #1,#4 (id clash) and #2 (dup) rejected


def test_invalid_posts_reported_not_invented():
    data = json.loads(_read("synthetic_topic_v1.json"))
    data["post_stream"]["posts"][0].pop("username")
    data["post_stream"]["posts"][1]["created_at"] = "yesterday"
    data["post_stream"]["posts"][2]["topic_id"] = 1
    thread = parse_capture(json.dumps(data), source_url=URL)
    assert sorted(e.post_number for e in thread.errors) == [1, 2, 3]
    assert [p.post_number for p in thread.posts] == [4]


def test_topic_id_mismatch_and_bad_json_fatal():
    with pytest.raises(ValidationError) as exc:
        parse_capture(_read("synthetic_topic_v1.json"), source_url="https://forum.example.invalid/t/s/999")
    assert exc.value.error_code == "community_topic_id_mismatch"
    with pytest.raises(ValidationError):
        parse_capture("{not json", source_url=URL)
    with pytest.raises(ValidationError):
        parse_capture('{"foo": 1}', source_url=URL)
    with pytest.raises(ValidationError):
        parse_capture("   ", source_url=URL)
    with pytest.raises(ValidationError):
        parse_capture("just prose, no posts", source_url=TEXT_URL)


def test_rendered_text_extraction():
    thread = parse_capture(_read("synthetic_topic.txt"), source_url=TEXT_URL)
    assert thread.source_format == "rendered_text" and thread.errors == []
    assert thread.topic.title == "SYNTHETIC: Rendered text capture" and thread.topic.category == "Synthetic Category"
    assert thread.topic.topic_id == 90002
    assert [(p.post_number, p.username, p.reply_to_post_number) for p in thread.posts] == [(1, "alice_dev", None), (2, "bob_partner", 1), (3, "alice_dev", 2)]
    assert thread.posts[0].body == "First post line one.\n\nSecond paragraph of the first post."
    assert thread.posts[2].updated_at == "2025-02-01T11:30:00+00:00"
    assert all(p.post_id is None and p.body_format == "text" for p in thread.posts)


def test_parser_never_calls_llm(monkeypatch):
    import llm

    def boom(*a, **k):  # pragma: no cover
        raise AssertionError("LLM must not be called")

    monkeypatch.setattr(llm, "chat_completion", boom)
    parse_capture(_read("synthetic_topic_v1.json"), source_url=URL)


# ------------------------------------------------------------ persistence


def test_initial_ingest_persists_records(store):
    before = _crm_snapshot(store)
    r = ingest_capture(store, content=_read("synthetic_topic_v1.json"), source_url=URL)
    assert (r.posts_processed, r.new_posts, r.edited_posts, r.unchanged_posts) == (4, 4, 0, 0)
    assert r.new_post_numbers == [1, 2, 3, 4] and r.command_id
    assert _count(store, "community_topics") == 1
    assert _count(store, "community_posts") == 4
    assert _count(store, "community_authors") == 3
    assert _count(store, "community_captures") == 1
    assert _count(store, "community_post_revisions") == 0
    with sqlite3.connect(store.database_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT p.post_number, p.post_id, a.username, p.post_url, p.created_at_src, p.reply_to_post_number "
            "FROM community_posts p JOIN community_authors a ON a.id = p.author_id ORDER BY p.post_number"
        ).fetchall()
        assert [(x["post_number"], x["post_id"], x["username"]) for x in rows] == [
            (1, 501, "alice_dev"), (2, 502, "bob_partner"), (3, 503, "alice_dev"), (4, 504, "carol_agency")]
        assert rows[2]["post_url"] == URL + "/3" and rows[2]["reply_to_post_number"] == 2
        assert rows[0]["created_at_src"] == "2025-01-10T09:00:00+00:00"
        cap = conn.execute("SELECT raw_payload, source_url FROM community_captures").fetchone()
        assert cap["raw_payload"] == _read("synthetic_topic_v1.json") and cap["source_url"] == URL
    assert _crm_snapshot(store) == before


def test_identical_reingest_creates_no_duplicates(store):
    content = _read("synthetic_topic_v1.json")
    ingest_capture(store, content=content, source_url=URL)
    snapshot = {t: _count(store, t) for t in ("community_topics", "community_posts", "community_authors", "community_captures", "community_post_revisions")}
    r = ingest_capture(store, content=content, source_url=URL)
    assert r.duplicate_capture and r.new_posts == 0 and r.edited_posts == 0 and r.unchanged_posts == 4
    assert {t: _count(store, t) for t in snapshot} == snapshot


def test_new_reply_and_edit_detected_with_revision(store):
    ingest_capture(store, content=_read("synthetic_topic_v1.json"), source_url=URL)
    r = ingest_capture(store, content=_read("synthetic_topic_v2.json"), source_url=URL)
    assert (r.posts_processed, r.new_posts, r.edited_posts, r.unchanged_posts) == (5, 1, 1, 3)
    assert r.new_post_numbers == [5] and r.edited_post_numbers == [3] and not r.incomplete_capture
    assert _count(store, "community_posts") == 5 and _count(store, "community_topics") == 1
    assert _count(store, "community_authors") == 3
    with sqlite3.connect(store.database_path) as conn:
        conn.row_factory = sqlite3.Row
        post = conn.execute("SELECT * FROM community_posts WHERE post_number = 3").fetchone()
        assert "EDIT: it worked" in post["body"] and post["revision_count"] == 1
        assert post["updated_at_src"] == "2025-01-10T12:00:00+00:00"
        rev = conn.execute("SELECT * FROM community_post_revisions").fetchall()
        assert len(rev) == 1 and rev[0]["body"] == "<p>Thanks, trying <b>that</b> now.</p>"
        assert rev[0]["post_row_id"] == post["id"]
    # replaying v2 and then v1 (older, already-seen capture) changes nothing
    again = ingest_capture(store, content=_read("synthetic_topic_v2.json"), source_url=URL)
    assert again.duplicate_capture
    old = ingest_capture(store, content=_read("synthetic_topic_v1.json"), source_url=URL)
    assert old.duplicate_capture and _count(store, "community_post_revisions") == 1


def test_incomplete_capture_never_deletes_posts(store):
    ingest_capture(store, content=_read("synthetic_topic_v2.json"), source_url=URL)
    partial = json.loads(_read("synthetic_topic_v2.json"))
    partial["post_stream"]["posts"] = partial["post_stream"]["posts"][:2]
    r = ingest_capture(store, content=json.dumps(partial), source_url=URL)
    assert (r.posts_processed, r.unchanged_posts, r.new_posts) == (2, 2, 0)
    assert _count(store, "community_posts") == 5


def test_incomplete_capture_flagged(store):
    partial = json.loads(_read("synthetic_topic_v2.json"))
    partial["post_stream"]["posts"] = partial["post_stream"]["posts"][:2]
    r = ingest_capture(store, content=json.dumps(partial), source_url=URL)
    assert r.incomplete_capture and any("incomplete" in w for w in r.warnings)


def test_rendered_text_reingest_and_edit(store):
    content = _read("synthetic_topic.txt")
    r1 = ingest_capture(store, content=content, source_url=TEXT_URL)
    assert r1.new_posts == 3
    assert ingest_capture(store, content=content, source_url=TEXT_URL).duplicate_capture
    edited = content.replace("A reply.", "A reply, amended.") + "\n#4 @carol_agency 2025-02-02T08:00:00Z\nLater.\n"
    r2 = ingest_capture(store, content=edited, source_url=TEXT_URL)
    assert (r2.new_posts, r2.edited_posts, r2.unchanged_posts) == (1, 1, 2)
    assert _count(store, "community_post_revisions") == 1 and _count(store, "community_posts") == 4


def test_conflicting_author_or_post_id_not_applied(store):
    ingest_capture(store, content=_read("synthetic_topic_v1.json"), source_url=URL)
    data = json.loads(_read("synthetic_topic_v1.json"))
    data["post_stream"]["posts"][1]["username"] = "mallory"
    data["post_stream"]["posts"][2]["id"] = 999
    data["post_stream"]["posts"][3]["cooked"] = "<p>changed</p>"
    r = ingest_capture(store, content=json.dumps(data), source_url=URL)
    assert {e.code for e in r.parse_errors} == {"author_conflict", "post_id_conflict"}
    assert r.edited_posts == 1 and r.unchanged_posts == 1
    with sqlite3.connect(store.database_path) as conn:
        assert conn.execute("SELECT username FROM community_authors WHERE username = 'mallory'").fetchone() is None


def test_failure_rolls_back_atomically(store, monkeypatch):
    calls = {"n": 0}
    real = community_db.insert_post

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("injected failure")
        return real(*a, **k)

    monkeypatch.setattr(community_db, "insert_post", flaky)
    with pytest.raises(RuntimeError):
        ingest_capture(store, content=_read("synthetic_topic_v1.json"), source_url=URL)
    for table in ("community_topics", "community_posts", "community_authors", "community_captures", "community_post_revisions"):
        assert _count(store, table) == 0, table
    monkeypatch.setattr(community_db, "insert_post", real)  # recovery: a retry succeeds fully
    r = ingest_capture(store, content=_read("synthetic_topic_v1.json"), source_url=URL)
    assert r.new_posts == 4 and _count(store, "community_posts") == 4


def test_ingest_does_not_touch_crm(store):
    import db

    lead, _ = db.upsert_lead({"company_name": "Existing Agency"}, db_path=store.database_path)
    db.add_person(lead["id"], {"name": "alice dev"}, db_path=store.database_path)
    before = _crm_snapshot(store)
    with sqlite3.connect(store.database_path) as conn:
        people_before = conn.execute("SELECT * FROM people").fetchall()
        leads_before = conn.execute("SELECT * FROM leads").fetchall()
    ingest_capture(store, content=_read("synthetic_topic_v1.json"), source_url=URL)
    ingest_capture(store, content=_read("synthetic_topic_v2.json"), source_url=URL)
    assert _crm_snapshot(store) == before
    with sqlite3.connect(store.database_path) as conn:
        assert conn.execute("SELECT * FROM people").fetchall() == people_before
        assert conn.execute("SELECT * FROM leads").fetchall() == leads_before


def test_postgres_runtime_fails_closed():
    class Pg:
        backend = "postgresql"

    with pytest.raises(community_db.CommunityRuntimeUnsupportedError):
        ingest_capture(Pg(), content=_read("synthetic_topic_v1.json"), source_url=URL)


# ------------------------------------------------------------------ routes


@pytest.fixture
def client(tmp_path):
    cfg = AppConfig(database_path=tmp_path / "routes.db", max_paste_chars=50_000)
    with TestClient(create_app(cfg), base_url="http://127.0.0.1:8025") as c:
        yield c


def test_community_page_ok_and_nav(client):
    r = client.get("/community")
    assert r.status_code == 200 and "No conversations ingested yet" in r.text
    for path in ("/", "/leads", "/ask", "/knowledge"):
        assert 'href="/community"' in client.get(path).text, path


def test_ingest_route_end_to_end(client):
    r = client.post("/community/ingest", data={"raw_text": _read("synthetic_topic_v1.json"), "source_url": URL})
    assert r.status_code == 200
    assert "processed 4" in r.text and "new 4" in r.text
    listing = client.get("/community")
    assert "SYNTHETIC: Checkout extension question" in listing.text
    detail = client.get("/community/topics/1")
    assert detail.status_code == 200
    body = detail.text
    assert body.index("#1") < body.index("#2") < body.index("#3") < body.index("#4")
    assert "@carol_agency" in body and "reply to #1" in body and URL + "/2" in body
    assert "We solved this for a client &amp; can share notes." in body  # html converted to text, then escaped

    again = client.post("/community/ingest", data={"raw_text": _read("synthetic_topic_v1.json"), "source_url": URL})
    assert "Identical capture already ingested" in again.text

    upd = client.post("/community/ingest", data={"raw_text": _read("synthetic_topic_v2.json"), "source_url": URL})
    assert "new 1" in upd.text and "edited 1" in upd.text and "Edited posts: #3" in upd.text
    detail = client.get("/community/topics/1").text
    assert "edited (1 revision)" in detail and "Previous versions" in detail and "#5" in detail


def test_ingest_route_validation_and_safety(client):
    assert client.post("/community/ingest", data={"raw_text": "", "source_url": URL}).status_code == 422
    assert client.post("/community/ingest", data={"raw_text": "x", "source_url": ""}).status_code == 422
    assert client.post("/community/ingest", data={"raw_text": "x", "source_url": "javascript:alert(1)"}).status_code == 422
    big = client.post("/community/ingest", data={"raw_text": "a" * 60_000, "source_url": URL})
    assert big.status_code == 422 and big.json()["error_code"] == "raw_text_too_long"
    bad = client.post("/community/ingest", data={"raw_text": _read("synthetic_topic_v1.json"), "source_url": URL},
                      headers={"Origin": "http://evil.example"})
    assert bad.status_code == 403
    assert client.post("/community/ingest", data={"raw_text": "{oops", "source_url": URL}).status_code == 422
    assert client.get("/community/topics/999").status_code == 404
    assert "No conversations" in client.get("/community").text  # nothing persisted by failures


def test_ingest_route_escapes_hostile_content(client):
    data = json.loads(_read("synthetic_topic_v1.json"))
    data["post_stream"]["posts"][0]["cooked"] = "<p>x</p><script>alert(1)</script>"
    data["title"] = "<img src=x onerror=alert(1)>"
    client.post("/community/ingest", data={"raw_text": json.dumps(data), "source_url": URL})
    page = client.get("/community/topics/1").text
    assert "<script>alert(1)</script>" not in page and "<img src=x" not in page
