from pathlib import Path

import db
from config import AppConfig
from providers.fake_gmail import FakeGmailProvider
from repositories.sqlite_store import SqliteContactStore
from services.gmail_sync_service import sync_gmail_full_mailbox


def test_page_failure_retains_cursor_and_resume_is_idempotent(tmp_path: Path):
    store = SqliteContactStore(tmp_path / "test.db"); store.init_db()
    cfg = AppConfig(database_path=store.database_path, gmail_enabled=True, gmail_full_sync_page_size=10)
    provider = FakeGmailProvider()
    for i in range(11): provider.seed_message(message_id=f"m{i}", thread_id=f"t{i}", subject="s", from_email="x@agency.test", to_email="operator@example.com", body="b")
    assert sync_gmail_full_mailbox(store, cfg, provider=provider, use_llm=False).status == "ok"
    provider.fail_page_token = "10"
    failed = sync_gmail_full_mailbox(store, cfg, provider=provider, use_llm=False)
    assert failed.error_code == "gmail_page_failed"
    provider.fail_page_token = None
    assert sync_gmail_full_mailbox(store, cfg, provider=provider, use_llm=False).status == "ok"
    with db.get_conn(store.database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM gmail_sources").fetchone()[0] == 11
        state = conn.execute("SELECT status, next_page_token, pages_processed FROM gmail_mailbox_sync_state").fetchone()
        assert state["status"] == "completed" and state["next_page_token"] is None and state["pages_processed"] == 2


def test_accounts_have_independent_cursors_and_message_identity(tmp_path: Path):
    cfg_path = tmp_path / "accounts.db"
    cfg = AppConfig(database_path=cfg_path, gmail_enabled=True, gmail_full_sync_page_size=10)
    store = SqliteContactStore(cfg_path); store.init_db()
    first = FakeGmailProvider("a@example.com"); second = FakeGmailProvider("b@example.com")
    for provider in (first, second):
        for i in range(11):
            provider.seed_message(message_id=f"same-{i}", thread_id="same-thread", subject="s", from_email="x@agency.test", to_email=provider.account_email, body="b")
    assert sync_gmail_full_mailbox(store, cfg, provider=first, use_llm=False, max_pages=1).status == "ok"
    assert sync_gmail_full_mailbox(store, cfg, provider=second, use_llm=False, max_pages=1).status == "ok"
    with db.get_conn(cfg_path) as conn:
        states = conn.execute("SELECT external_account, next_page_token, pages_processed FROM gmail_mailbox_sync_state ORDER BY external_account").fetchall()
        assert [(row["external_account"], row["next_page_token"], row["pages_processed"]) for row in states] == [("a@example.com", "10", 1), ("b@example.com", "10", 1)]
        assert conn.execute("SELECT COUNT(*) FROM gmail_sources").fetchone()[0] == 20
        assert conn.execute("SELECT COUNT(*) FROM gmail_conversations").fetchone()[0] == 2


def test_completed_rerun_does_not_inflate_rows_or_projection_counts(tmp_path: Path):
    store = SqliteContactStore(tmp_path / "rerun.db"); store.init_db()
    cfg = AppConfig(database_path=store.database_path, gmail_enabled=True, gmail_full_sync_page_size=10)
    provider = FakeGmailProvider()
    for i in range(3): provider.seed_message(message_id=f"m{i}", thread_id="t", subject="s", from_email="x@agency.test", to_email="operator@example.com", body="b")
    assert sync_gmail_full_mailbox(store, cfg, provider=provider, use_llm=False).status == "ok"
    with db.get_conn(store.database_path) as conn:
        before = tuple(conn.execute("SELECT (SELECT COUNT(*) FROM gmail_sources), (SELECT COUNT(*) FROM gmail_messages), (SELECT COUNT(*) FROM gmail_conversations)").fetchone())
    assert sync_gmail_full_mailbox(store, cfg, provider=provider, use_llm=False).status == "ok"
    with db.get_conn(store.database_path) as conn:
        assert tuple(conn.execute("SELECT (SELECT COUNT(*) FROM gmail_sources), (SELECT COUNT(*) FROM gmail_messages), (SELECT COUNT(*) FROM gmail_conversations)").fetchone()) == before


def test_individual_fetch_failure_keeps_other_messages_and_advances_page(tmp_path: Path):
    class Broken(FakeGmailProvider):
        def get_message(self, message_id):
            if message_id == "broken":
                raise RuntimeError("raw provider detail")
            return super().get_message(message_id)
    store = SqliteContactStore(tmp_path / "message-failure.db"); store.init_db()
    cfg = AppConfig(database_path=store.database_path, gmail_enabled=True, gmail_full_sync_page_size=10)
    provider = Broken()
    for message_id in ("good-1", "good-2", "broken", "good-3"):
        provider.seed_message(message_id=message_id, thread_id=message_id, subject="s", from_email="x@agency.test", to_email="operator@example.com", body="b")
    result = sync_gmail_full_mailbox(store, cfg, provider=provider, use_llm=False)
    assert result.status == "ok" and result.counts.failed == 1
    with db.get_conn(store.database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM gmail_sources").fetchone()[0] == 3
        assert conn.execute("SELECT status, next_page_token FROM gmail_mailbox_sync_state").fetchone()["status"] == "completed"
