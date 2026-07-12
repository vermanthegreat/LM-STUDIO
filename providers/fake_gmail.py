"""Deterministic fake Gmail provider for tests."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Optional

from gmail_schemas import EmailAddress, NormalizedGmailMessage


class FakeGmailProvider:
    def __init__(self, account_email: str = "operator@example.com") -> None:
        self.account_email = account_email.lower()
        self._labels = [
            {"id": "Label_LMStudio", "name": "LMStudio", "type": "user"},
            {"id": "INBOX", "name": "INBOX", "type": "system"},
        ]
        self._messages: dict[str, dict[str, Any]] = {}
        self._threads: dict[str, list[str]] = {}

    def seed_message(
        self,
        *,
        message_id: str,
        thread_id: str,
        subject: str,
        from_email: str,
        to_email: str,
        body: str,
        internal_date: Optional[datetime] = None,
        label_ids: Optional[list[str]] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> None:
        when = internal_date or datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
        header_list = [
            {"name": "From", "value": from_email},
            {"name": "To", "value": to_email},
            {"name": "Subject", "value": subject},
            {"name": "Date", "value": when.strftime("%a, %d %b %Y %H:%M:%S +0000")},
        ]
        for key, value in (headers or {}).items():
            header_list.append({"name": key, "value": value})
        encoded = __import__("base64").urlsafe_b64encode(body.encode("utf-8")).decode("ascii")
        api_message = {
            "id": message_id,
            "threadId": thread_id,
            "labelIds": label_ids or ["Label_LMStudio"],
            "internalDate": str(int(when.timestamp() * 1000)),
            "snippet": body[:80],
            "payload": {
                "mimeType": "text/plain",
                "headers": header_list,
                "body": {"data": encoded},
            },
        }
        self._messages[message_id] = api_message
        self._threads.setdefault(thread_id, [])
        if message_id not in self._threads[thread_id]:
            self._threads[thread_id].append(message_id)

    def get_account_profile(self) -> dict[str, Any]:
        return {"email": self.account_email, "messages_total": len(self._messages), "threads_total": len(self._threads)}

    def list_labels(self) -> list[dict[str, Any]]:
        return deepcopy(self._labels)

    def resolve_label_id(self, label_name: str) -> Optional[str]:
        target = label_name.strip().lower()
        for label in self._labels:
            if (label.get("name") or "").lower() == target:
                return str(label["id"])
        return None

    def list_messages(
        self,
        label_id: str,
        limit: int,
        page_token: Optional[str] = None,
    ) -> dict[str, Any]:
        del page_token
        ids = [
            mid
            for mid, msg in self._messages.items()
            if label_id in (msg.get("labelIds") or [])
        ]
        ids = ids[: max(1, limit)]
        return {"messages": [{"id": mid} for mid in ids], "resultSizeEstimate": len(ids)}

    def get_message(self, message_id: str) -> NormalizedGmailMessage:
        from providers.gmail_normalize import normalize_gmail_api_message

        api_message = self._messages[message_id]
        return normalize_gmail_api_message(self.account_email, api_message)

    def get_thread(self, thread_id: str) -> list[NormalizedGmailMessage]:
        from providers.gmail_normalize import normalize_gmail_api_message

        messages = []
        for message_id in self._threads.get(thread_id, []):
            messages.append(normalize_gmail_api_message(self.account_email, self._messages[message_id]))
        messages.sort(key=lambda item: item.internal_date)
        return messages

    def seed_html_message(
        self,
        *,
        message_id: str,
        thread_id: str,
        subject: str,
        from_email: str,
        to_email: str,
        html_body: str,
    ) -> None:
        when = datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc)
        encoded = __import__("base64").urlsafe_b64encode(html_body.encode("utf-8")).decode("ascii")
        self._messages[message_id] = {
            "id": message_id,
            "threadId": thread_id,
            "labelIds": ["Label_LMStudio"],
            "internalDate": str(int(when.timestamp() * 1000)),
            "payload": {
                "mimeType": "text/html",
                "headers": [
                    {"name": "From", "value": from_email},
                    {"name": "To", "value": to_email},
                    {"name": "Subject", "value": subject},
                ],
                "body": {"data": encoded},
            },
        }
        self._threads.setdefault(thread_id, []).append(message_id)
