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
