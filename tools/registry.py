"""Application-owned typed tool registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Type

from pydantic import BaseModel, ValidationError

from repositories import ContactStore
from tools.envelope import ToolResult
from tools.read_handlers import (
    handle_calculate_pipeline_analytics,
    handle_find_companies_missing_email,
    handle_list_due_followups,
    handle_search_contacts,
)
from tools.read_inputs import (
    CalculatePipelineAnalyticsInput,
    FindCompaniesMissingEmailInput,
    ListDueFollowupsInput,
    SearchContactsInput,
)
from tools.risk import RiskClass
from tools.write_handlers import handle_propose_contact_update, handle_propose_create_followup
from tools.write_inputs import ProposeContactUpdateInput, ProposeCreateFollowupInput


class ToolRegistryError(Exception):
    """Base registry error."""


class UnknownToolError(ToolRegistryError):
    """Raised when a tool name is not registered."""


class ToolValidationError(ToolRegistryError):
    """Raised when tool arguments fail schema validation."""


Handler = Callable[[ContactStore, BaseModel], ToolResult]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    risk_class: RiskClass
    input_model: Type[BaseModel]
    handler: Handler
    description: str = ""


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ToolRegistryError(f"Tool already registered: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        spec = self._tools.get(name)
        if spec is None:
            raise UnknownToolError(f"Unknown tool: {name}")
        return spec

    def list_tools(self) -> list[str]:
        return sorted(self._tools)

    def validate_arguments(self, name: str, arguments: dict[str, Any]) -> BaseModel:
        spec = self.get(name)
        try:
            return spec.input_model.model_validate(arguments)
        except ValidationError as exc:
            raise ToolValidationError(str(exc)) from exc

    def execute(
        self,
        store: ContactStore,
        name: str,
        arguments: dict[str, Any],
    ) -> ToolResult:
        spec = self.get(name)
        validated = self.validate_arguments(name, arguments)
        return spec.handler(store, validated)


def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="search_contacts",
            risk_class=RiskClass.READ,
            input_model=SearchContactsInput,
            handler=handle_search_contacts,
            description="Read-only contact and organization search.",
        )
    )
    registry.register(
        ToolSpec(
            name="find_companies_missing_email",
            risk_class=RiskClass.READ,
            input_model=FindCompaniesMissingEmailInput,
            handler=handle_find_companies_missing_email,
            description="Read-only companies missing email by definition.",
        )
    )
    registry.register(
        ToolSpec(
            name="list_due_followups",
            risk_class=RiskClass.READ,
            input_model=ListDueFollowupsInput,
            handler=handle_list_due_followups,
            description="Read-only open follow-up tasks.",
        )
    )
    registry.register(
        ToolSpec(
            name="calculate_pipeline_analytics",
            risk_class=RiskClass.READ,
            input_model=CalculatePipelineAnalyticsInput,
            handler=handle_calculate_pipeline_analytics,
            description="Read-only deterministic pipeline metrics.",
        )
    )
    registry.register(
        ToolSpec(
            name="propose_create_followup",
            risk_class=RiskClass.PROPOSE,
            input_model=ProposeCreateFollowupInput,
            handler=handle_propose_create_followup,
            description="Propose a follow-up task without committing it.",
        )
    )
    registry.register(
        ToolSpec(
            name="propose_contact_update",
            risk_class=RiskClass.PROPOSE,
            input_model=ProposeContactUpdateInput,
            handler=handle_propose_contact_update,
            description="Propose a single company contact-field update without committing it.",
        )
    )
    return registry
