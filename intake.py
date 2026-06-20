"""Parse intake validation for pasted contact text."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import db
from errors import ValidationError
from models import SOURCE_TYPES
from repositories.sqlite_store import SqliteContactStore

_ALLOWED_SOURCE_TYPES = frozenset(SOURCE_TYPES)


@dataclass(frozen=True)
class ParseIntake:
    source_type: str
    raw_text: str
    source_url: Optional[str]
    attach_to_lead_id: Optional[int]


def _validate_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValidationError(
            error_code="invalid_source_url",
            message="Source URL must be a valid http or https address.",
            status_code=422,
        )
    return url.strip()


def validate_parse_intake(
    *,
    source_type: str,
    raw_text: str,
    source_url: str,
    attach_to_lead_id: str,
    max_paste_chars: int,
    store=None,
    database_path: Path | None = None,
) -> ParseIntake:
    st = (source_type or "").strip()
    if st not in _ALLOWED_SOURCE_TYPES:
        raise ValidationError(
            error_code="invalid_source_type",
            message=f"Source type must be one of: {', '.join(SOURCE_TYPES)}.",
            status_code=422,
        )

    text = (raw_text or "").strip()
    if not text:
        raise ValidationError(
            error_code="empty_raw_text",
            message="Pasted text cannot be empty.",
            status_code=422,
        )
    if len(text) > max_paste_chars:
        raise ValidationError(
            error_code="raw_text_too_long",
            message=f"Pasted text exceeds the maximum of {max_paste_chars} characters.",
            status_code=422,
        )

    url_value: Optional[str] = None
    if source_url and source_url.strip():
        url_value = _validate_url(source_url)

    lead_id: Optional[int] = None
    attach = (attach_to_lead_id or "").strip()
    if attach:
        if not re.fullmatch(r"\d+", attach):
            raise ValidationError(
                error_code="invalid_attach_lead_id",
                message="Attach-to-lead id must be a positive integer.",
                status_code=422,
            )
        lead_id = int(attach)
        if store is None:
            store = SqliteContactStore(database_path) if database_path else SqliteContactStore(db.DB_PATH)
        if not store.get_lead(lead_id):
            raise ValidationError(
                error_code="lead_not_found",
                message=f"Lead {lead_id} was not found.",
                status_code=404,
            )

    return ParseIntake(
        source_type=st,
        raw_text=text,
        source_url=url_value,
        attach_to_lead_id=lead_id,
    )
