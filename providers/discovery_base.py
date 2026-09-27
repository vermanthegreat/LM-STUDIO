"""Narrow provider-neutral discovery protocol."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from discovery_models import DiscoveryOutcome, DiscoveryRequest


@runtime_checkable
class DiscoveryProvider(Protocol):
    def execute(self, request: DiscoveryRequest) -> DiscoveryOutcome:
        ...
