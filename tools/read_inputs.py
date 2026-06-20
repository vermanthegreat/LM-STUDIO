"""Pydantic input models for Phase 2 read tools."""

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class MissingEmailDefinition(str, Enum):
    ANY = "any"
    NON_REJECTED = "non_rejected"
    VERIFIED = "verified"


class SearchContactsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: Optional[str] = None
    organization_status: Optional[str] = None
    minimum_relevance: Optional[int] = Field(default=None, ge=0, le=100)
    has_person: Optional[bool] = None
    limit: int = Field(default=25, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class FindCompaniesMissingEmailInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_status: Optional[str] = None
    minimum_relevance: Optional[int] = Field(default=None, ge=0, le=100)
    missing_definition: MissingEmailDefinition = MissingEmailDefinition.ANY
    limit: int = Field(default=50, ge=1, le=200)


class ListDueFollowupsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Optional[str] = None
    priority: Optional[str] = None
    limit: int = Field(default=50, ge=1, le=200)


class PipelineMetric(str, Enum):
    ORGANIZATION_COUNT = "organization_count"
    CONTACT_COVERAGE = "contact_coverage"
    VERIFIED_EMAIL_COVERAGE = "verified_email_coverage"
    OVERDUE_TASK_COUNT = "overdue_task_count"


class CalculatePipelineAnalyticsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: PipelineMetric
