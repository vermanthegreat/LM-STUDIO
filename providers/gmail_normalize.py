"""Deterministic Gmail API payload normalization."""

from __future__ import annotations

import base64
import hashlib
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Optional

from gmail_schemas import EmailAddress, NormalizedGmailMessage

_HEADER_RE = re.compile(r"^([\w-]+):\s*(.*)$", re.MULTILINE)


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self._parts.append(text)

    def get_text(self) -> str:
        return "\n".join(self._parts)


def html_to_text(html: str) -> str:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)
    return parser.get_text()


def _decode_body_data(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    return raw.decode("utf-8", errors="replace")


def _parse_address(raw: Optional[str]) -> EmailAddress:
    text = (raw or "").strip()
    if not text:
        return EmailAddress(email="")
    angle = re.search(r"<([^>]+@[^>]+)>", text)
    if angle:
        email = angle.group(1).strip().lower()
        name = text[: angle.start()].strip().strip('"').strip("'") or None
        return EmailAddress(email=email, display_name=name)
    email_match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", text)
    if email_match:
        email = email_match.group(0).lower()
        name = text.replace(email, "").strip().strip('"').strip("'") or None
        return EmailAddress(email=email, display_name=name)
    return EmailAddress(email=text.lower())


def _parse_address_list(raw: Optional[str]) -> list[EmailAddress]:
    if not raw:
        return []
    parts = re.split(r",(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", raw)
    return [_parse_address(part.strip()) for part in parts if part.strip()]


def _header_map(headers: list[dict[str, str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in headers or []:
        name = (item.get("name") or "").lower()
        value = item.get("value") or ""
        if name:
            out[name] = value
    return out


def _walk_parts(
    payload: dict[str, Any],
    *,
    plain_parts: list[str],
    html_parts: list[str],
    attachments: list[dict[str, Any]],
) -> None:
    mime = payload.get("mimeType") or ""
    body = payload.get("body") or {}
    data = body.get("data")
    if data:
        decoded = _decode_body_data(data)
        if mime == "text/plain":
            plain_parts.append(decoded)
        elif mime == "text/html":
            html_parts.append(decoded)
    for part in payload.get("parts") or []:
        filename = part.get("filename") or ""
        if filename:
            part_body = part.get("body") or {}
            attachments.append(
                {
                    "filename": filename,
                    "mime_type": part.get("mimeType"),
                    "size": part_body.get("size"),
                    "attachment_id": part_body.get("attachmentId"),
                }
            )
        _walk_parts(part, plain_parts=plain_parts, html_parts=html_parts, attachments=attachments)


def _extract_body(payload: dict[str, Any]) -> tuple[Optional[str], Optional[str], list[dict[str, Any]]]:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[dict[str, Any]] = []
    _walk_parts(payload, plain_parts=plain_parts, html_parts=html_parts, attachments=attachments)
    plain = "\n\n".join(p.strip() for p in plain_parts if p.strip()) or None
    if not plain and html_parts:
        plain = html_to_text("\n".join(html_parts))
    mime = payload.get("mimeType")
    return plain, mime, attachments


def _parse_internal_date(raw: Any) -> datetime:
    if raw is None:
        return datetime.now(timezone.utc)
    try:
        ms = int(raw)
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def _parse_date_header(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def normalize_gmail_api_message(
    account_email: str,
    api_message: dict[str, Any],
    *,
    full_format: bool = True,
) -> NormalizedGmailMessage:
    payload = api_message.get("payload") or {}
    headers = _header_map(payload.get("headers") or [])
    plain_body, mime_type, attachments = _extract_body(payload) if full_format else (None, None, [])
    internal = _parse_internal_date(api_message.get("internalDate"))
    date_header = headers.get("date")
    occurred = _parse_date_header(date_header) or internal
    return NormalizedGmailMessage(
        account_email=account_email.lower(),
        message_id=str(api_message.get("id") or ""),
        thread_id=str(api_message.get("threadId") or ""),
        internal_date=occurred,
        rfc_message_id=headers.get("message-id"),
        from_address=_parse_address(headers.get("from")),
        to_addresses=_parse_address_list(headers.get("to")),
        cc_addresses=_parse_address_list(headers.get("cc")),
        bcc_addresses=_parse_address_list(headers.get("bcc")),
        reply_to=_parse_address(headers.get("reply-to")) if headers.get("reply-to") else None,
        subject=headers.get("subject"),
        date_header=date_header,
        in_reply_to=headers.get("in-reply-to"),
        references=headers.get("references"),
        label_ids=list(api_message.get("labelIds") or []),
        mime_type=mime_type,
        plain_body=plain_body,
        provider_metadata={
            "snippet": api_message.get("snippet"),
            "history_id": api_message.get("historyId"),
            "size_estimate": api_message.get("sizeEstimate"),
        },
        attachment_metadata=attachments,
    )


def content_hash_for_message(message: NormalizedGmailMessage) -> str:
    digest = hashlib.sha256()
    digest.update(message.message_id.encode("utf-8"))
    digest.update((message.subject or "").encode("utf-8"))
    digest.update((message.plain_body or "").encode("utf-8"))
    digest.update(message.internal_date.isoformat().encode("utf-8"))
    return digest.hexdigest()
