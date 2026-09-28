"""Typed contracts for the knowledge subsystem."""

from __future__ import annotations

import re
from datetime import date
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ContentKind(str, Enum):
    TEXT = "text"
    DOCUMENT = "document"
    IMAGE = "image"
    UNSUPPORTED = "unsupported"


class ExtractionStatus(str, Enum):
    OK = "ok"
    EMPTY = "empty"
    NEEDS_VISION = "needs_vision"
    FAILED = "failed"


class VisionStatus(str, Enum):
    NOT_APPLICABLE = "not_applicable"
    UNAVAILABLE = "unavailable"
    OK = "ok"
    FAILED = "failed"


class ClassificationStatus(str, Enum):
    OK = "ok"
    UNAVAILABLE = "unavailable"
    INVALID_OUTPUT = "invalid_output"
    SKIPPED_NO_TEXT = "skipped_no_text"
    DISABLED = "disabled"


class IngestStatus(str, Enum):
    INGESTED = "ingested"
    DUPLICATE = "duplicate"
    UNSUPPORTED = "unsupported"
    REJECTED = "rejected"
    FAILED = "failed"


class EntityType(str, Enum):
    PERSON = "person"
    ORGANIZATION = "organization"
    PRODUCT = "product"
    PLACE = "place"
    OTHER = "other"


class LinkStatus(str, Enum):
    LINKED = "linked"
    AMBIGUOUS = "ambiguous"
    UNLINKED = "unlinked"
    NOT_APPLICABLE = "not_applicable"


_WS_RE = re.compile(r"\s+")


def _clean_label(value: Any, max_len: int) -> Optional[str]:
    if value is None:
        return None
    text = _WS_RE.sub(" ", str(value)).strip()
    if not text or text.lower() in {"null", "none", "n/a", "unknown"}:
        return None
    return text[:max_len]


class KnowledgeEntity(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=200)
    type: EntityType = EntityType.OTHER

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, value: Any) -> str:
        cleaned = _clean_label(value, 200)
        if not cleaned:
            raise ValueError("entity name is empty")
        return cleaned

    @field_validator("type", mode="before")
    @classmethod
    def _type(cls, value: Any) -> EntityType:
        try:
            return EntityType(str(value or "other").strip().lower())
        except ValueError:
            return EntityType.OTHER


class KnowledgeClassification(BaseModel):
    """Advisory model output. Validated before any persistence."""

    model_config = ConfigDict(extra="ignore")

    summary: Optional[str] = None
    project: Optional[str] = None
    category: Optional[str] = None
    sub_category: Optional[str] = None
    topics: list[str] = Field(default_factory=list)
    entities: list[KnowledgeEntity] = Field(default_factory=list)
    event_date: Optional[date] = None
    importance: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("summary", mode="before")
    @classmethod
    def _summary(cls, value: Any) -> Optional[str]:
        return _clean_label(value, 1500)

    @field_validator("project", "category", "sub_category", mode="before")
    @classmethod
    def _labels(cls, value: Any) -> Optional[str]:
        return _clean_label(value, 120)

    @field_validator("topics", mode="before")
    @classmethod
    def _topics(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("topics must be a list")
        out: list[str] = []
        seen: set[str] = set()
        for item in value:
            cleaned = _clean_label(item, 60)
            if cleaned and cleaned.lower() not in seen:
                seen.add(cleaned.lower())
                out.append(cleaned)
        return out[:12]

    @field_validator("entities", mode="before")
    @classmethod
    def _entities(cls, value: Any) -> list[Any]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("entities must be a list")
        out: list[Any] = []
        for item in value:
            if isinstance(item, str):
                item = {"name": item, "type": "other"}
            if isinstance(item, dict) and _clean_label(item.get("name"), 200):
                out.append(item)
        return out[:30]

    @field_validator("event_date", mode="before")
    @classmethod
    def _event_date(cls, value: Any) -> Optional[date]:
        """Accept ISO YYYY-MM-DD only; anything vaguer is dropped, not guessed."""
        if value is None or value == "":
            return None
        if isinstance(value, date):
            return value
        text = str(value).strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return None
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None


class VisionResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    visible_text: str = ""
    description: str = ""
    image_type: Literal[
        "text_screenshot", "ui_screenshot", "photo", "diagram", "whiteboard", "chart", "document_scan", "other"
    ] = "other"

    @field_validator("visible_text", "description", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str:
        return str(value or "").strip()[:20000]

    @field_validator("image_type", mode="before")
    @classmethod
    def _image_type(cls, value: Any) -> str:
        allowed = {
            "text_screenshot", "ui_screenshot", "photo", "diagram", "whiteboard", "chart", "document_scan", "other",
        }
        text = str(value or "other").strip().lower()
        return text if text in allowed else "other"


class FileIngestResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str
    status: IngestStatus
    item_id: Optional[int] = None
    existing_item_id: Optional[int] = None
    content_kind: Optional[ContentKind] = None
    mime_type: Optional[str] = None
    content_hash: Optional[str] = None
    extraction_status: Optional[ExtractionStatus] = None
    vision_status: Optional[VisionStatus] = None
    classification_status: Optional[ClassificationStatus] = None
    error_code: Optional[str] = None
    message: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)
    preview: Optional[dict[str, Any]] = None


class BatchIngestResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "partial", "error"]
    command_id: Optional[str] = None
    counts: dict[str, int]
    results: list[FileIngestResult]


class KnowledgeSearchFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: Optional[str] = Field(default=None, max_length=120)
    category: Optional[str] = Field(default=None, max_length=120)
    topic: Optional[str] = Field(default=None, max_length=60)
    entity: Optional[str] = Field(default=None, max_length=200)
    person_id: Optional[int] = Field(default=None, ge=1)
    content_kind: Optional[ContentKind] = None
    event_from: Optional[date] = None
    event_to: Optional[date] = None
    captured_from: Optional[date] = None
    captured_to: Optional[date] = None
    # Effective date = event_date when known, else the capture date.
    date_from: Optional[date] = None
    date_to: Optional[date] = None


class SearchKnowledgeInput(KnowledgeSearchFilters):
    query: str = Field(default="", max_length=500)
    limit: int = Field(default=10, ge=1, le=50)
