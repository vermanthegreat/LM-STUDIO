"""Pydantic models for validated LLM extraction output."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ExtractedPerson(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    title: Optional[str] = None
    linkedin_url: Optional[str] = None
    department: Optional[str] = None
    email: Optional[str] = None
    email_status: Optional[str] = None
    email_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class ExtractedInteraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: Optional[str] = None
    summary: Optional[str] = None
    reply_needed: Optional[bool] = None
    deadline: Optional[str] = None
    next_action: Optional[str] = None


class ExtractionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_name: Optional[str] = None
    website: Optional[str] = None
    partner_tier: Optional[str] = None
    services: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    description: Optional[str] = None
    people: list[ExtractedPerson] = Field(default_factory=list)
    interaction: Optional[ExtractedInteraction] = None
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)


def try_validate_extraction(data: dict[str, Any]) -> Optional[ExtractionOutput]:
    """Return validated extraction output, or None when the payload is untrusted."""
    try:
        return ExtractionOutput.model_validate(data)
    except ValidationError:
        return None


def extraction_to_dict(output: ExtractionOutput) -> dict[str, Any]:
    return output.model_dump(mode="json")
