"""Explicit production composition for bounded research providers."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Optional

from discovery_models import DiscoveryOutcome
from discovery_materialization_models import DiscoveryOutcomeMaterializationResult
from repositories import ContactStore
from services.company_website_discovery_provider import CompanyWebsiteDiscoveryProvider
from services.company_website_http_transport import (
    CompanyWebsiteClock,
    CompanyWebsiteConnectionFactory,
    CompanyWebsiteDnsResolver,
    HardenedCompanyWebsiteHttpTransport,
)
from services.discovery_outcome_materialization import materialize_discovery_outcome
from services.research_job_runner import DiscoveryOutcomeMaterializerPort, ResearchJobRunner


@dataclass(frozen=True)
class CompanyWebsiteTransportDependencies:
    """Optional low-level dependencies used for deterministic isolated tests."""

    resolver: Optional[CompanyWebsiteDnsResolver] = None
    connection_factory: Optional[CompanyWebsiteConnectionFactory] = None
    clock: Optional[CompanyWebsiteClock] = None


class SQLiteDiscoveryOutcomeMaterializerAdapter(DiscoveryOutcomeMaterializerPort):
    """Nominal runner port over the existing discovery materialization service."""

    def __init__(self, repository: ContactStore) -> None:
        self._repository = repository

    def materialize_discovery_outcome(
        self,
        *,
        research_job_id: int,
        lease_token: str,
        expected_version: int,
        outcome: DiscoveryOutcome,
    ) -> DiscoveryOutcomeMaterializationResult:
        return materialize_discovery_outcome(
            self._repository,
            research_job_id=research_job_id,
            lease_token=lease_token,
            expected_version=expected_version,
            outcome=outcome,
        )


def build_research_job_runner(
    *,
    repository: ContactStore,
    worker_id: str,
    network_dependencies: Optional[CompanyWebsiteTransportDependencies] = None,
) -> ResearchJobRunner:
    """Build the inert one-shot company-website research stack."""

    if not isinstance(worker_id, str) or not worker_id.strip():
        raise ValueError("worker ID is required")
    dependencies = network_dependencies or CompanyWebsiteTransportDependencies()
    transport = HardenedCompanyWebsiteHttpTransport(
        resolver=dependencies.resolver,
        connection_factory=dependencies.connection_factory,
        clock=dependencies.clock,
    )
    provider = CompanyWebsiteDiscoveryProvider(transport)
    providers = MappingProxyType({"company_website": provider})
    materializer = SQLiteDiscoveryOutcomeMaterializerAdapter(repository)
    return ResearchJobRunner(repository, providers, materializer=materializer)
