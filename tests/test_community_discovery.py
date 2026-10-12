"""Community discovery (M2) tests. Fixtures are SYNTHETIC (tests/fixtures/community/README.md)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from community_discovery import normalize_source, parse_discovery
from config import AppConfig
from errors import ValidationError
from fastapi.testclient import TestClient
from repositories.sqlite_store import SqliteContactStore
from services import community_discovery_service as svc
from services.community_ingest_service import ingest_capture

from app import create_app

FIX = Path(__file__).parent / "fixtures" / "community"
HOST = "forum.example.invalid"
LIST_URL = f"https://{HOST}/latest"
SEARCH_URL = f"https://{HOST}/search?q=checkout"
TOPIC_URL = f"https://{HOST}/t/synthetic-checkout-extension-question/90001"


def _read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


@pytest.fixture
def store(tmp_path):
    s = SqliteContactStore(tmp_path / "disc.db")
    s.init_db()
    return s


def _count(store, table: str) -> int:
    with sqlite3.connect(store.database_path) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _queue_ids(store) -> list[int]:
    return [q["topic_id"] for q in svc.work_queue(store)]


# ------------------------------------------------------------------ parser


def test_normalize_source_ignores_page_and_json_suffix():
    assert normalize_source("https://H.example/latest.json?page=3")[1:] == ("h.example", "/latest")
    assert normalize_source("https://h.example/search?q=a&page=2")[2] == "/search?q=a"
    with pytest.raises(ValidationError):
        normalize_source("ftp://h/latest")


def test_parse_listing_and_search_shapes():
    listing = parse_discovery(_read("synthetic_listing_p1.json"), source_url=LIST_URL)
    assert listing.kind == "listing" and listing.source_key == "listing:/latest"
    assert [t.topic_id for t in listing.topics] == [1, 90001, 90010, 90011]
    assert listing.topics[0].pinned and listing.topics[1].url == TOPIC_URL
    assert listing.topics[1].last_posted_at == "2025-02-01T10:00:00+00:00"
    search = parse_discovery(_read("synthetic_search.json"), source_url=SEARCH_URL)
    assert search.kind == "search" and search.source_key == "search:/search?q=checkout"


def test_parse_rejects_unrecognized_and_flags_bad_rows():
    for bad in ("not json", "[]", '{"x": 1}'):
        with pytest.raises(ValidationError):
            parse_discovery(bad, source_url=LIST_URL)
    data = {"topic_list": {"topics": [{"id": "x"}, {"id": 5, "slug": "a"}, {"id": 5, "slug": "a"}]}}
    parsed = parse_discovery(json.dumps(data), source_url=LIST_URL)
    assert [t.topic_id for t in parsed.topics] == [5]
    assert [e.code for e in parsed.errors] == ["invalid_topic_id"]
    assert any("more than once" in w for w in parsed.warnings)


# ------------------------------------------------------- discovery persistence


def test_first_discovery_marks_everything_new_with_provenance(store):
    r = svc.record_discovery(store, content=_read("synthetic_listing_p1.json"), source_url=LIST_URL)
    assert r.new_topics == [1, 90001, 90010, 90011] and not r.pending_topics
    assert r.high_water == "2025-02-01T10:00:00+00:00" and r.previous_high_water is None
    assert r.command_id
    assert _count(store, "community_discovered_topics") == 4
    with sqlite3.connect(store.database_path) as conn:
        run = conn.execute("SELECT kind, source_url, raw_payload FROM community_discovery_runs").fetchall()
        assert run[0][0] == "listing" and run[0][1] == LIST_URL and "SYNTHETIC" in run[0][2]
        assert conn.execute("SELECT COUNT(*) FROM community_discovery_sightings").fetchone()[0] == 4


def test_identical_capture_is_a_noop(store):
    content = _read("synthetic_listing_p1.json")
    svc.record_discovery(store, content=content, source_url=LIST_URL)
    again = svc.record_discovery(store, content=content, source_url=LIST_URL)
    assert again.duplicate_capture and again.run_id
    assert (_count(store, "community_discovery_runs"), _count(store, "community_discovered_topics")) == (1, 4)
    assert _count(store, "community_discovery_sightings") == 4


def test_known_vs_new_detection_across_runs_and_sources(store):
    svc.record_discovery(store, content=_read("synthetic_listing_p1.json"), source_url=LIST_URL)
    later = svc.record_discovery(store, content=_read("synthetic_listing_later.json"), source_url=LIST_URL)
    assert later.new_topics == [90020]
    assert later.pending_topics == [90010, 90011]
    s = svc.record_discovery(store, content=_read("synthetic_search.json"), source_url=SEARCH_URL)
    assert s.new_topics == [90030] and s.pending_topics == [90010]
    assert _count(store, "community_discovered_topics") == 6  # no duplicate topic rows
    with sqlite3.connect(store.database_path) as conn:
        times = dict(conn.execute("SELECT topic_id, times_seen FROM community_discovered_topics").fetchall())
        assert times[90010] == 3 and times[90020] == 1
        first = conn.execute(
            "SELECT first_run_id, last_run_id FROM community_discovered_topics WHERE topic_id = 90010"
        ).fetchone()
        assert first[0] < first[1]


def test_ingested_topic_detected_and_update_available(store):
    ingest_capture(store, content=_read("synthetic_topic_v1.json"), source_url=TOPIC_URL)  # 4 posts
    r = svc.record_discovery(store, content=_read("synthetic_listing_p1.json"), source_url=LIST_URL)
    assert r.update_available_topics == [90001]  # listing says 5 posts, 4 stored
    assert 90001 in _queue_ids(store)
    ingest_capture(store, content=_read("synthetic_topic_v2.json"), source_url=TOPIC_URL)  # 5 posts
    assert 90001 not in _queue_ids(store)
    again = svc.record_discovery(store, content=_read("synthetic_listing_later.json"), source_url=LIST_URL)
    assert 90001 not in again.update_available_topics


def test_counts_only_move_forward(store):
    svc.record_discovery(store, content=_read("synthetic_listing_later.json"), source_url=LIST_URL)
    svc.record_discovery(store, content=_read("synthetic_search.json"), source_url=SEARCH_URL)  # 90010 has 2 < 6
    with sqlite3.connect(store.database_path) as conn:
        row = conn.execute(
            "SELECT posts_count_seen, last_posted_at_seen FROM community_discovered_topics WHERE topic_id = 90010"
        ).fetchone()
    assert row == (6, "2025-02-20T10:00:00+00:00")


# --------------------------------------------------------- checkpoints / resume


def test_checkpoint_persists_across_store_instances_and_detects_overlap(store):
    first = svc.record_discovery(store, content=_read("synthetic_listing_p1.json"), source_url=LIST_URL + "?page=0")
    assert not first.reached_checkpoint
    reopened = SqliteContactStore(store.database_path)  # simulates an app restart
    cps = svc.list_checkpoints(reopened)
    assert len(cps) == 1 and cps[0]["source_key"] == "listing:/latest"
    assert cps[0]["high_water_last_posted_at"] == "2025-02-01T10:00:00+00:00"
    later = svc.record_discovery(reopened, content=_read("synthetic_listing_later.json"), source_url=LIST_URL)
    assert later.previous_high_water == "2025-02-01T10:00:00+00:00"
    assert later.reached_checkpoint  # 90011 (2025-01-28) is at/below the old high-water mark
    assert later.high_water == "2025-03-01T10:00:00+00:00"
    assert svc.list_checkpoints(reopened)[0]["runs_count"] == 2


def test_pinned_old_topic_does_not_fake_checkpoint_reached(store):
    svc.record_discovery(store, content=_read("synthetic_listing_p1.json"), source_url=LIST_URL)
    only_new = {"topic_list": {"topics": [
        {"id": 1, "slug": "pinned-welcome", "posts_count": 3, "last_posted_at": "2024-01-01T00:00:00Z", "pinned": True},
        {"id": 95000, "slug": "brand-new", "posts_count": 1, "last_posted_at": "2025-06-01T00:00:00Z"},
    ]}}
    r = svc.record_discovery(store, content=json.dumps(only_new), source_url=LIST_URL)
    assert r.new_topics == [95000] and not r.reached_checkpoint


def test_resume_after_interruption_continues_queue(store):
    svc.record_discovery(store, content=_read("synthetic_listing_p1.json"), source_url=LIST_URL)
    svc.record_discovery(store, content=_read("synthetic_listing_p2.json"), source_url=LIST_URL + "?page=1")
    assert _queue_ids(store) == [90001, 90010, 90011, 90012, 90013, 1]  # newest activity first
    ingest_capture(store, content=_read("synthetic_topic_v2.json"), source_url=TOPIC_URL)
    # "Restart": brand-new store object on the same file; queue comes from the database alone.
    reopened = SqliteContactStore(store.database_path)
    assert _queue_ids(reopened) == [90010, 90011, 90012, 90013, 1]
    # Re-recording an already-recorded page after the restart changes nothing.
    again = svc.record_discovery(reopened, content=_read("synthetic_listing_p2.json"), source_url=LIST_URL + "?page=1")
    assert again.duplicate_capture and _queue_ids(reopened) == [90010, 90011, 90012, 90013, 1]


def test_failed_run_rolls_back_completely(store, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(svc, "_classify", boom)
    with pytest.raises(RuntimeError):
        svc.record_discovery(store, content=_read("synthetic_listing_p1.json"), source_url=LIST_URL)
    for table in ("community_discovery_runs", "community_discovered_topics",
                  "community_discovery_sightings", "community_discovery_checkpoints"):
        assert _count(store, table) == 0
    monkeypatch.undo()
    ok = svc.record_discovery(store, content=_read("synthetic_listing_p1.json"), source_url=LIST_URL)
    assert len(ok.new_topics) == 4  # retry after the interruption succeeds


def test_discovery_does_not_touch_crm_or_conversation_tables(store):
    before = {t: _count(store, t) for t in ("leads", "people", "interactions", "tasks", "community_topics", "community_posts")}
    svc.record_discovery(store, content=_read("synthetic_listing_p1.json"), source_url=LIST_URL)
    assert {t: _count(store, t) for t in before} == before


# ------------------------------------------------------------------ routes


@pytest.fixture
def client(tmp_path):
    cfg = AppConfig(database_path=tmp_path / "web.db")
    app = create_app(cfg)
    with TestClient(app, base_url=f"http://127.0.0.1:{cfg.port}") as c:
        yield c


def test_discover_route_and_queue_render(client):
    r = client.post("/community/discover", data={"source_url": LIST_URL, "raw_text": _read("synthetic_listing_p1.json")})
    assert r.status_code == 200
    assert "Discovery result" in r.text and "Waiting to ingest (4)" in r.text
    r = client.post("/community/discover", data={"source_url": LIST_URL, "raw_text": _read("synthetic_listing_p1.json")})
    assert "already recorded" in r.text
    r = client.get("/community")
    assert "Waiting to ingest (4)" in r.text and "listing:/latest" in r.text


def test_discover_route_validation(client):
    r = client.post("/community/discover", data={"source_url": "", "raw_text": "{}"})
    assert r.status_code == 422
    r = client.post("/community/discover", data={"source_url": LIST_URL, "raw_text": "not json"})
    assert r.status_code == 422


# ------------------------------------------------------------ user summary (SYNTHETIC)

SUMMARY_URL = f"https://{HOST}/u/synthetic_user/summary"


def _summary() -> str:
    return _read("synthetic_user_summary.json")


def test_parse_user_summary_extracts_identity_topics_replies_stats():
    p = parse_discovery(_summary(), source_url=SUMMARY_URL)
    assert p.kind == "user_summary" and p.username == "synthetic_user"
    assert p.source_key == "user_summary:/u/synthetic_user/summary"
    ids = [t.topic_id for t in p.topics]
    assert len(ids) == 13 and ids == sorted(ids) and ids[0] == 90001
    assert p.topics[0].title == "SYNTHETIC: Checkout extension question"
    assert p.topics[0].url == TOPIC_URL and p.topics[0].posts_count == 5
    assert p.topics[-1].title is None  # referenced only; nothing invented
    assert [(r.topic_id, r.post_number) for r in p.replies] == [(90001, 3), (90001, 9), (91011, 2), (91012, 4)]
    assert p.stats["post_count"] == 14 and "can_see_summary_stats" not in p.stats
    assert p.errors == [] and p.warnings == []


def test_user_summary_requires_identifiable_user_and_flags_bad_replies():
    with pytest.raises(ValidationError) as e:
        parse_discovery(_summary(), source_url=f"https://{HOST}/latest")
    assert e.value.error_code == "community_user_summary_username_unknown"
    data = json.loads(_summary())
    data["user_summary"]["replies"] += [{"topic_id": 5}, {"topic_id": 90001, "post_number": 3}]
    p = parse_discovery(json.dumps(data), source_url=SUMMARY_URL)
    assert [x.code for x in p.errors] == ["invalid_reply_reference"] and any("more than once" in w for w in p.warnings)
    data["users"] = [{"username": "someone_else"}]
    assert any("unconfirmed" in w for w in parse_discovery(json.dumps(data), source_url=SUMMARY_URL).warnings)


def test_user_summary_persists_provenance_replies_and_stats(store):
    r = svc.record_discovery(store, content=_summary(), source_url=SUMMARY_URL)
    assert r.kind == "user_summary" and len(r.new_topics) == 13 and r.new_replies == 4 and r.command_id
    with sqlite3.connect(store.database_path) as conn:
        assert conn.execute("SELECT kind, source_url FROM community_discovery_runs").fetchone() == ("user_summary", SUMMARY_URL)
        assert conn.execute("SELECT COUNT(*) FROM community_discovered_topics").fetchone()[0] == 13
        assert conn.execute("SELECT COUNT(*) FROM community_user_replies WHERE username_norm='synthetic_user'").fetchone()[0] == 4
        assert json.loads(conn.execute("SELECT stats_json FROM community_user_activity").fetchone()[0])["days_visited"] == 12
        assert conn.execute("SELECT COUNT(*) FROM community_discovery_sightings").fetchone()[0] == 13


def test_user_summary_idempotent_and_dedupes_known_topics(store):
    svc.record_discovery(store, content=_read("synthetic_listing_p1.json"), source_url=LIST_URL)  # overlaps the summary at 90001 only
    first = svc.record_discovery(store, content=_summary(), source_url=SUMMARY_URL)
    assert 90001 in first.pending_topics and 90001 not in first.new_topics and len(first.new_topics) == 12
    again = svc.record_discovery(store, content=_summary(), source_url=SUMMARY_URL)
    assert again.duplicate_capture
    # A changed summary (one more reply) adds only the new reference, never duplicates.
    data = json.loads(_summary())
    data["user_summary"]["replies"].append({"topic_id": 91001, "post_number": 2})
    changed = svc.record_discovery(store, content=json.dumps(data), source_url=SUMMARY_URL)
    assert changed.new_replies == 1 and changed.new_topics == []
    assert _count(store, "community_user_replies") == 5
    assert _count(store, "community_discovered_topics") == 4 + 12  # listing's 4 + 12 new; 90001 not duplicated
    assert _count(store, "community_user_activity") == 1


def test_topic_status_pending_partial_complete_and_resume(store):
    svc.record_discovery(store, content=_summary(), source_url=SUMMARY_URL)
    status = {t["topic_id"]: t for t in svc.topic_statuses(store)}
    assert {s["status"] for s in status.values()} == {"pending"} and len(status) == 13
    assert [q["topic_id"] for q in svc.work_queue(store)] == sorted(status)  # deterministic
    assert svc.next_topic(store)["topic_id"] == 90001

    # v1 capture: 4 posts of the 5 the summary reports; reply #3 stored, reply #9 not.
    ingest_capture(store, content=_read("synthetic_topic_v1.json"), source_url=TOPIC_URL)
    t = {x["topic_id"]: x for x in svc.topic_statuses(store)}[90001]
    assert t["status"] == "partial" and t["posts_stored"] == 4 and t["missing_reply_posts"] == [9]
    assert 90001 in [q["topic_id"] for q in svc.work_queue(store)]

    # "Restart": fresh store object; interrupted work resumes from the database alone.
    reopened = SqliteContactStore(store.database_path)
    assert svc.next_topic(reopened)["topic_id"] == 90001
    ingest_capture(reopened, content=_read("synthetic_topic_v2.json"), source_url=TOPIC_URL)  # 5 posts
    t = {x["topic_id"]: x for x in svc.topic_statuses(reopened)}[90001]
    assert t["missing_reply_posts"] == [9] and t["status"] == "partial"  # post 9 still not in capture
    assert len(svc.work_queue(reopened)) == 13
    # Re-ingesting the same capture does not duplicate anything.
    ingest_capture(reopened, content=_read("synthetic_topic_v2.json"), source_url=TOPIC_URL)
    assert _count(reopened, "community_posts") == 5 and _count(reopened, "community_topics") == 1


def test_complete_and_coverage_unknown_statuses(store):
    data = json.loads(_summary())
    data["user_summary"]["replies"] = [r for r in data["user_summary"]["replies"] if r["post_number"] != 9]
    svc.record_discovery(store, content=json.dumps(data), source_url=SUMMARY_URL)
    ingest_capture(store, content=_read("synthetic_topic_v2.json"), source_url=TOPIC_URL)
    t = {x["topic_id"]: x for x in svc.topic_statuses(store)}[90001]
    assert t["status"] == "complete" and t["missing_reply_posts"] == []
    assert 90001 not in [q["topic_id"] for q in svc.work_queue(store)]
    # A topic whose source reported no post count is never called "complete".
    nocount = {"topic_list": {"topics": [{"id": 90001, "slug": "synthetic-checkout-extension-question"}]}}
    s2 = SqliteContactStore(store.database_path.parent / "other.db"); s2.init_db()
    svc.record_discovery(s2, content=json.dumps(nocount), source_url=LIST_URL)
    ingest_capture(s2, content=_read("synthetic_topic_v1.json"), source_url=TOPIC_URL)
    assert svc.topic_statuses(s2)[0]["status"] == "coverage_unknown"


def test_user_summary_route_renders_queue_status(client):
    r = client.post("/community/discover", data={"source_url": SUMMARY_URL, "raw_text": _summary()})
    assert r.status_code == 200
    assert "synthetic_user" in r.text and "Reply references: 4 in capture, 4 new" in r.text
    assert "Waiting to ingest (13)" in r.text and "not ingested" in r.text
