"""Deterministic, side-effect-free discovery provider for contract tests."""

from __future__ import annotations

from copy import deepcopy
from typing import Iterable

from discovery_models import DiscoveryOutcome, DiscoveryOutcomeStatus, DiscoveryRequest


class FakeDiscoveryProvider:
    """Return configured outcomes without network, filesystem, or database access."""

    def __init__(self, outcomes: DiscoveryOutcome | Iterable[DiscoveryOutcome]) -> None:
        if isinstance(outcomes, DiscoveryOutcome):
            self._outcomes = [deepcopy(outcomes)]
            self._repeat = True
        else:
            self._outcomes = [deepcopy(outcome) for outcome in outcomes]
            self._repeat = False
        if not self._outcomes:
            raise ValueError("fake provider requires at least one outcome")
        self.requests: list[DiscoveryRequest] = []
        self._index = 0

    def execute(self, request: DiscoveryRequest) -> DiscoveryOutcome:
        if not isinstance(request, DiscoveryRequest):
            raise TypeError("request must be a DiscoveryRequest")
        self.requests.append(request.model_copy(deep=True))
        if self._repeat:
            outcome = self._outcomes[0]
        else:
            outcome = self._outcomes[min(self._index, len(self._outcomes) - 1)]
            self._index += 1
        outcome = deepcopy(outcome)
        outcome.provider_request_id = f"fake-{len(self.requests):04d}"
        outcome.validate_for_request(request)
        if outcome.status == DiscoveryOutcomeStatus.SUCCEEDED and not outcome.candidates:
            raise ValueError("fake succeeded outcome requires candidates")
        return outcome
