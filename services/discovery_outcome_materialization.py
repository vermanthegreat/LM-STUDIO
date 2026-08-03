"""Persistence boundary for already-validated discovery outcomes."""

from __future__ import annotations

from typing import Any

from discovery_models import DiscoveryOutcome
from discovery_materialization_models import DiscoveryOutcomeMaterializationResult


def materialize_discovery_outcome(
    repository: Any,
    *,
    research_job_id: int,
    lease_token: str,
    expected_version: int,
    outcome: DiscoveryOutcome,
) -> DiscoveryOutcomeMaterializationResult:
    """Materialize one validated outcome through the repository boundary."""

    if not hasattr(repository, "materialize_discovery_outcome"):
        raise TypeError("repository does not support discovery materialization")
    return repository.materialize_discovery_outcome(
        research_job_id=research_job_id,
        lease_token=lease_token,
        expected_version=expected_version,
        outcome=outcome,
    )


class DiscoveryOutcomeMaterializer:
    """Small injectable facade for callers that prefer an object boundary."""

    def __init__(self, repository: Any) -> None:
        self.repository = repository

    def materialize_discovery_outcome(
        self,
        *,
        research_job_id: int,
        lease_token: str,
        expected_version: int,
        outcome: DiscoveryOutcome,
    ) -> DiscoveryOutcomeMaterializationResult:
        return materialize_discovery_outcome(
            self.repository,
            research_job_id=research_job_id,
            lease_token=lease_token,
            expected_version=expected_version,
            outcome=outcome,
        )
