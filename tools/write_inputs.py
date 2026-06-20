"""Pydantic input models for Phase 3 write-proposal tools."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class ProposeCreateFollowupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lead_id: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    due_date: Optional[str] = None
    priority: Optional[str] = Field(default=None, max_length=50)
    idempotency_key: Optional[str] = Field(default=None, max_length=128)


class ContactUpdateField(str, Enum):
    COMPANY_EMAIL = "company_email"
    COMPANY_PHONE = "company_phone"
    WEBSITE = "website"


class ProposeContactUpdateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lead_id: int = Field(ge=1)
    field: ContactUpdateField
    value: str = Field(min_length=1, max_length=500)
    idempotency_key: Optional[str] = Field(default=None, max_length=128)
