"""Gate 1 scenario contract: planner questions must actually reach the planner."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from ask_router import _deterministic_intent
from tools.registry import build_default_registry

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "certify_gate1_planner.py"
_spec = importlib.util.spec_from_file_location("certify_gate1_planner", _SCRIPT)
gate1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate1)


@pytest.mark.parametrize("scenario", gate1.PLANNER_SCENARIOS, ids=lambda s: s["id"])
def test_planner_scenarios_bypass_deterministic_routing(scenario):
    assert _deterministic_intent(scenario["question"]).name == "unknown"


@pytest.mark.parametrize("scenario", gate1.PLANNER_SCENARIOS, ids=lambda s: s["id"])
def test_planner_scenario_expectations_are_valid_tool_arguments(scenario):
    registry = build_default_registry()
    registry.validate_arguments(scenario["expected_tool"], scenario["expected_arguments"])


def test_p1_contract_is_unambiguous_and_strict():
    p1 = next(s for s in gate1.PLANNER_SCENARIOS if s["id"] == "P1")
    assert p1["question"] == (
        "Which agencies have no inbox address of any kind on record, verified or unverified?"
    )
    assert p1["expected_tool"] == "find_companies_missing_email"
    assert p1["expected_arguments"] == {"missing_definition": "any"}


def test_deterministic_control_does_not_reach_planner():
    assert _deterministic_intent(gate1.CONTROL["question"]).name == gate1.CONTROL["expected_intent"]


def test_unsafe_scenario_reaches_planner():
    assert _deterministic_intent(gate1.UNSAFE["question"]).name == "unknown"
