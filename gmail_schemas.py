"""Gmail G0 typed contracts — enums and Pydantic models."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


class EmailDirection(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    INTERNAL = "internal"
    UNKNOWN = "unknown"


class LinkStatus(str, Enum):
    LINKED = "linked"
    UNLINKED = "unlinked"
    AMBIGUOUS = "ambiguous"


class GmailMessageRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    thread_id: Optional[str] = None


class GmailMessagePage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    messages: list[GmailMessageRef] = Field(default_factory=list)
    next_page_token: Optional[str] = None
    result_size_estimate: Optional[int] = None


class PrimaryIntent(str, Enum):
    OUTREACH = "outreach"
    POSITIVE_INTEREST = "positive_interest"
    NEGATIVE_OR_DECLINE = "negative_or_decline"
    QUESTION_OR_INFORMATION_REQUEST = "question_or_information_request"
    MEETING_OR_CALL_REQUEST = "meeting_or_call_request"
    FOLLOW_UP_OR_REMINDER = "follow_up_or_reminder"
    INFORMATIONAL = "informational"
    AUTOMATED = "automated"
    UNKNOWN = "unknown"


class MessageRole(str, Enum):
    CONVERSATION_MESSAGE = "conversation_message"
    SHOPIFY_PARTNER_INQUIRY_CONFIRMATION = "shopify_partner_inquiry_confirmation"


class AttentionMarker(str, Enum):
    REPLY_NEEDED = "reply_needed"
    FOLLOW_UP_NEEDED = "follow_up_needed"
    MEETING_REQUESTED = "meeting_requested"
    DEADLINE_PRESENT = "deadline_present"
    TIME_REFERENCE_PRESENT = "time_reference_present"
    POSITIVE_SIGNAL = "positive_signal"
    NEGATIVE_SIGNAL = "negative_signal"
    AUTOMATED_MESSAGE = "automated_message"
    UNMATCHED_SENDER = "unmatched_sender"
    AMBIGUOUS_CONTACT_MATCH = "ambiguous_contact_match"
    NEEDS_OPERATOR_REVIEW = "needs_operator_review"
    CALENDAR_CANDIDATE = "calendar_candidate"


class ClassificationSource(str, Enum):
    DETERMINISTIC = "deterministic"
    LOCAL_LLM = "local_llm"
    FALLBACK = "fallback"


class TemporalKind(str, Enum):
    MEETING_TIME = "meeting_time"
    DEADLINE = "deadline"
    FOLLOWUP_TIME = "followup_time"
    AVAILABILITY = "availability"
    OTHER = "other"


class TemporalResolutionStatus(str, Enum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"


class TemporalSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: TemporalKind
    raw_text: str
    normalized_start_at: Optional[datetime] = None
    normalized_end_at: Optional[datetime] = None
    timezone: Optional[str] = None
    resolution_status: TemporalResolutionStatus
    confidence: float = Field(ge=0.0, le=1.0)


class EmailAddress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str
    display_name: Optional[str] = None


class NormalizedGmailMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_email: str
    message_id: str
    thread_id: str
    internal_date: datetime
    rfc_message_id: Optional[str] = None
    from_address: EmailAddress
    to_addresses: list[EmailAddress] = Field(default_factory=list)
    cc_addresses: list[EmailAddress] = Field(default_factory=list)
    bcc_addresses: list[EmailAddress] = Field(default_factory=list)
    reply_to: Optional[EmailAddress] = None
    subject: Optional[str] = None
    date_header: Optional[str] = None
    in_reply_to: Optional[str] = None
    references: Optional[str] = None
    label_ids: list[str] = Field(default_factory=list)
    mime_type: Optional[str] = None
    plain_body: Optional[str] = None
    provider_metadata: dict[str, Any] = Field(default_factory=dict)
    attachment_metadata: list[dict[str, Any]] = Field(default_factory=list)


class EmailClassificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_role: MessageRole = MessageRole.CONVERSATION_MESSAGE
    target_company_name: Optional[str] = None
    primary_intent: PrimaryIntent
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(max_length=500)
    markers: list[AttentionMarker] = Field(default_factory=list)
    temporal_signals: list[TemporalSignal] = Field(default_factory=list)
    classification_source: ClassificationSource
    classification_model: Optional[str] = None
    classification_warning: Optional[str] = None

    @field_validator("markers", mode="before")
    @classmethod
    def _validate_markers(cls, value: Any) -> list[AttentionMarker]:
        if not value:
            return []
        out: list[AttentionMarker] = []
        for item in value:
            if isinstance(item, AttentionMarker):
                out.append(item)
            else:
                out.append(AttentionMarker(str(item)))
        return out


class SyncResultCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    discovered: int = 0
    imported: int = 0
    updated: int = 0
    already_present: int = 0
    linked: int = 0
    unlinked: int = 0
    ambiguous: int = 0
    classification_failed: int = 0
    failed: int = 0


class GmailSyncResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "error"]
    counts: SyncResultCounts
    warnings: list[str] = Field(default_factory=list)
    error_code: Optional[str] = None
    message: Optional[str] = None


def derive_requires_followup(markers: list[AttentionMarker]) -> bool:
    followup_markers = {
        AttentionMarker.REPLY_NEEDED,
        AttentionMarker.FOLLOW_UP_NEEDED,
        AttentionMarker.MEETING_REQUESTED,
        AttentionMarker.DEADLINE_PRESENT,
    }
    return any(marker in followup_markers for marker in markers)


def validate_markers(raw: list[str]) -> list[AttentionMarker]:
    allowed = {m.value for m in AttentionMarker}
    return [AttentionMarker(m) for m in raw if m in allowed]
