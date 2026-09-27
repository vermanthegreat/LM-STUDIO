"""Typed results for durable discovery-outcome materialization."""

from __future__ import annotations

import re
from typing import Tuple

from pydantic import BaseModel, ConfigDict, Field

from discovery_models import DiscoveryOutcomeStatus


class DiscoveryOutcomeMaterializationResult(BaseModel):
    """Immutable, bounded receipt and linkage identifiers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    materialization_id: int = Field(gt=0)
    research_job_id: int = Field(gt=0)
    attempt_count: int = Field(gt=0)
    outcome_status: DiscoveryOutcomeStatus
    outcome_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_source_ids: Tuple[int, ...] = ()
    person_candidate_ids: Tuple[int, ...] = ()
    contact_candidate_ids: Tuple[int, ...] = ()
    replayed: bool = False
