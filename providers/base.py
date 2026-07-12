"""Narrow provider protocols for external integrations."""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from gmail_schemas import NormalizedGmailMessage


@runtime_checkable
class GmailProvider(Protocol):
    def get_account_profile(self) -> dict[str, Any]: ...

    def list_labels(self) -> list[dict[str, Any]]: ...

    def list_messages(
        self,
        label_id: str,
        limit: int,
        page_token: Optional[str] = None,
    ) -> dict[str, Any]: ...

    def get_message(self, message_id: str) -> NormalizedGmailMessage: ...

    def get_thread(self, thread_id: str) -> list[NormalizedGmailMessage]: ...
