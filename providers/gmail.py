"""Read-only Gmail API adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from gmail_schemas import GMAIL_READONLY_SCOPE, NormalizedGmailMessage
from providers.gmail_normalize import normalize_gmail_api_message

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
except ImportError:  # pragma: no cover - optional until deps installed
    Request = None  # type: ignore
    Credentials = None  # type: ignore
    InstalledAppFlow = None  # type: ignore
    build = None  # type: ignore


class GmailConfigurationError(Exception):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


class GmailProviderAdapter:
    """Narrow read-only Gmail provider; does not expose the underlying client."""

    def __init__(self, credentials: Any, account_email: str) -> None:
        self._account_email = account_email.lower()
        self._service = build("gmail", "v1", credentials=credentials, cache_discovery=False)

    @property
    def account_email(self) -> str:
        return self._account_email

    def get_account_profile(self) -> dict[str, Any]:
        profile = self._service.users().getProfile(userId="me").execute()
        return {
            "email": profile.get("emailAddress", self._account_email),
            "messages_total": profile.get("messagesTotal"),
            "threads_total": profile.get("threadsTotal"),
        }

    def list_labels(self) -> list[dict[str, Any]]:
        response = self._service.users().labels().list(userId="me").execute()
        return list(response.get("labels") or [])

    def resolve_label_id(self, label_name: str) -> Optional[str]:
        target = label_name.strip().lower()
        for label in self.list_labels():
            name = (label.get("name") or "").strip().lower()
            if name == target:
                return str(label.get("id"))
        return None

    def list_messages(
        self,
        label_id: str,
        limit: int,
        page_token: Optional[str] = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "userId": "me",
            "labelIds": [label_id],
            "maxResults": max(1, min(limit, 500)),
        }
        if page_token:
            kwargs["pageToken"] = page_token
        return self._service.users().messages().list(**kwargs).execute()

    def get_message(self, message_id: str) -> NormalizedGmailMessage:
        api_message = (
            self._service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        return normalize_gmail_api_message(self._account_email, api_message)

    def get_thread(self, thread_id: str) -> list[NormalizedGmailMessage]:
        thread = (
            self._service.users()
            .threads()
            .get(userId="me", id=thread_id, format="full")
            .execute()
        )
        messages = []
        for api_message in thread.get("messages") or []:
            messages.append(normalize_gmail_api_message(self._account_email, api_message))
        messages.sort(key=lambda item: item.internal_date)
        return messages


def load_credentials(token_path: Path) -> Any:
    if Credentials is None:
        raise GmailConfigurationError(
            "gmail_dependencies_missing",
            "Google API client libraries are not installed.",
        )
    if not token_path.is_file():
        raise GmailConfigurationError(
            "gmail_token_missing",
            f"Gmail token file not found at configured path.",
        )
    creds = Credentials.from_authorized_user_file(str(token_path), [GMAIL_READONLY_SCOPE])
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
    if not creds or not creds.valid:
        raise GmailConfigurationError(
            "gmail_token_invalid",
            "Gmail token is missing or invalid. Run the authorization script.",
        )
    return creds


def build_gmail_provider(
    *,
    client_secret_path: Path,
    token_path: Path,
) -> GmailProviderAdapter:
    creds = load_credentials(token_path)
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    profile = service.users().getProfile(userId="me").execute()
    email = (profile.get("emailAddress") or "").lower()
    if not email:
        raise GmailConfigurationError("gmail_profile_unavailable", "Could not read Gmail account profile.")
    return GmailProviderAdapter(creds, email)


def run_local_authorization(client_secret_path: Path, token_path: Path) -> dict[str, str]:
    if InstalledAppFlow is None:
        raise GmailConfigurationError(
            "gmail_dependencies_missing",
            "Google API client libraries are not installed.",
        )
    if not client_secret_path.is_file():
        raise GmailConfigurationError(
            "gmail_client_secret_missing",
            "Gmail client secret file not found at configured path.",
        )
    flow = InstalledAppFlow.from_client_secrets_file(
        str(client_secret_path),
        scopes=[GMAIL_READONLY_SCOPE],
    )
    creds = flow.run_local_server(port=0)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    profile = service.users().getProfile(userId="me").execute()
    email = profile.get("emailAddress") or "unknown"
    return {"status": "authorized", "account_email": email}
