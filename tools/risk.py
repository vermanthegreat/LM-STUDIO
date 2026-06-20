"""Tool risk classification per docs/tool-contracts.md."""

from __future__ import annotations

from enum import Enum


class RiskClass(str, Enum):
    READ = "read"
    PROPOSE = "propose"
    WRITE = "write"
    BULK_WRITE = "bulk_write"
    DESTRUCTIVE = "destructive"
    EXTERNAL = "external"

    @property
    def requires_approval(self) -> bool:
        return self in {
            RiskClass.WRITE,
            RiskClass.BULK_WRITE,
            RiskClass.DESTRUCTIVE,
            RiskClass.EXTERNAL,
        }

    @property
    def creates_write_proposal(self) -> bool:
        return self == RiskClass.PROPOSE
