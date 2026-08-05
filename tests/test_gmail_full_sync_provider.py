from datetime import datetime, timezone

from config import AppConfig
from providers.fake_gmail import FakeGmailProvider


def _provider() -> FakeGmailProvider:
    provider = FakeGmailProvider()
    for i in range(12):
        provider.seed_message(message_id=f"m{i}", thread_id=f"t{i}", subject="s", from_email="a@agency.test", to_email="operator@example.com", body="b", internal_date=datetime(2026, 1, 1, tzinfo=timezone.utc), label_ids=["INBOX"])
    provider.seed_message(message_id="spam", thread_id="spam", subject="s", from_email="s@x.test", to_email="operator@example.com", body="b", label_ids=["SPAM"])
    provider.seed_message(message_id="trash", thread_id="trash", subject="s", from_email="t@x.test", to_email="operator@example.com", body="b", label_ids=["TRASH"])
    return provider


def test_page_bounds_pagination_and_spam_trash_exclusion():
    provider = _provider()
    first = provider.list_message_page(page_token=None, max_results=1)
    assert len(first.messages) == 10 and first.next_page_token == "10"
    second = provider.list_message_page(page_token=first.next_page_token, max_results=500)
    assert [ref.id for ref in second.messages] == ["m10", "m11"] and second.next_page_token is None
    assert {ref.thread_id for ref in first.messages} == {f"t{i}" for i in range(10)}
    assert "spam" not in {r.id for r in first.messages + second.messages}
    assert "trash" not in {r.id for r in first.messages + second.messages}


def test_default_full_sync_page_size_is_100_and_fake_failure_is_deterministic():
    assert AppConfig().gmail_full_sync_page_size == 100
    provider = _provider(); provider.fail_page_token = "10"
    try:
        provider.list_message_page(page_token="10", max_results=10)
    except RuntimeError as exc:
        assert str(exc) == "injected_page_failure"
    else:
        raise AssertionError("expected injected failure")
