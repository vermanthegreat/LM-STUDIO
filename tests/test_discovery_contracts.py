"""Focused tests for bounded provider-neutral discovery contracts."""

from datetime import datetime, timedelta, timezone
import socket
import sqlite3

import pytest
from pydantic import ValidationError

from candidate_models import DISCOVERY_METHODS, normalize_candidate_name, normalize_contact_value
from discovery_models import (
    DiscoveryCandidate,
    DiscoveryContact,
    DiscoveryOutcome,
    DiscoveryOutcomeStatus,
    DiscoveryRequest,
    DiscoverySource,
    content_hash,
)
from providers.fake_discovery import FakeDiscoveryProvider


NOW = datetime(2026, 8, 3, tzinfo=timezone.utc)


def request(**overrides):
    data = {
        "job_id": "job-1",
        "lead_id": 1,
        "company_name": "Example Agency",
        "normalized_domain": "example.com",
        "company_website": "https://example.com",
        "target_roles": ["economic_buyer", "economic_buyer"],
        "result_limit": 2,
        "approved_source_types": ["website"],
        "requested_at": NOW,
        "requester_identity": "operator",
        "correlation_id": "corr-1",
        "provider_config_ref": "fake-default",
        "max_pages": 2,
        "max_requests": 2,
        "timeout_seconds": 10,
    }
    data.update(overrides)
    return DiscoveryRequest(**data)


def source():
    return DiscoverySource(
        source_url="https://example.com/about",
        canonical_url="https://example.com/about",
        source_type="website",
        page_title="About",
        retrieved_at=NOW,
        content_hash=content_hash("evidence"),
        extracted_text="Visible evidence",
        http_status=200,
        content_type="text/html",
        provider_request_id="provider-1",
    )


def candidate():
    return DiscoveryCandidate(
        name="Jane Doe",
        normalized_name="jane doe",
        title="Founder",
        role_type="economic_buyer",
        source_url="https://example.com/about",
        source_type="website",
        confidence=0.8,
        evidence_basis="source_confirmed",
        discovery_method="deterministic_parser",
    )


def outcome(status=DiscoveryOutcomeStatus.SUCCEEDED, **overrides):
    data = {
        "provider_name": "fake",
        "provider_request_id": "provider-1",
        "status": status,
        "started_at": NOW,
        "completed_at": NOW + timedelta(seconds=1),
        "sources": [source()],
        "candidates": [candidate()] if status in (DiscoveryOutcomeStatus.SUCCEEDED, DiscoveryOutcomeStatus.PARTIAL) else [],
        "warnings": [],
    }
    data.update(overrides)
    return DiscoveryOutcome(**data)


def test_request_is_bounded_and_deduplicates_roles():
    assert request().target_roles == ["economic_buyer"]


@pytest.mark.parametrize("field,value", [
    ("lead_id", 0),
    ("normalized_domain", "https://example.com/path"),
    ("company_website", "https://user:password@example.com"),
    ("target_roles", ["unsupported"]),
    ("result_limit", 6),
    ("max_pages", 6),
    ("max_requests", 6),
    ("timeout_seconds", 31),
])
def test_request_rejects_invalid_bounds(field, value):
    with pytest.raises((ValidationError, ValueError)):
        request(**{field: value})


def test_request_rejects_secret_keys_without_echoing_value():
    with pytest.raises(ValidationError) as exc:
        request(provider_config_ref={"api_key": "do-not-echo"})
    assert "do-not-echo" not in str(exc.value)


def test_source_metadata_and_hash_are_bounded():
    assert source().content_hash == content_hash("evidence")
    with pytest.raises(ValidationError):
        DiscoverySource(**{**source().model_dump(), "extracted_text": "x" * 20_001})


def test_contact_truthfulness_rejects_verified_and_inferred_source_confirmation():
    base = {"kind": "email", "value": "jane@example.com", "normalized_value": "jane@example.com", "source_url": "https://example.com"}
    with pytest.raises(ValidationError):
        DiscoveryContact(**base, evidence_basis="source_confirmed", verification_status="verified")
    with pytest.raises(ValidationError):
        DiscoveryContact(**base, evidence_basis="inferred", verification_status="source_confirmed")


def test_contact_normalization_uses_authoritative_rules():
    values = {
        "email": (" Jane@Example.com ", "jane@example.com"),
        "phone": ("+1 (555) 123", "1555123"),
        "linkedin": ("HTTPS://Example.com/Jane/", "https://example.com/jane"),
        "website": ("HTTPS://Example.com/", "https://example.com"),
        "other": ("  Preferred  channel ", "preferred channel"),
    }
    for kind, (value, normalized) in values.items():
        contact = DiscoveryContact(
            kind=kind,
            value=value,
            normalized_value=normalized,
            evidence_basis="source_confirmed",
            verification_status="unverified",
            source_url="https://example.com",
        )
        assert contact.normalized_value == normalize_contact_value(kind, value)


def test_contact_normalization_rejects_mismatch_without_echoing_value():
    secret = "Jane@Example.com"
    with pytest.raises(ValidationError) as exc:
        DiscoveryContact(
            kind="email",
            value=secret,
            normalized_value="other@example.com",
            evidence_basis="source_confirmed",
            verification_status="unverified",
            source_url="https://example.com",
        )
    assert secret not in str(exc.value)
    with pytest.raises(ValidationError):
        DiscoveryContact(
            kind="linkedin",
            value="https://example.com/jane",
            normalized_value="https://example.com/john",
            evidence_basis="source_confirmed",
            verification_status="unverified",
            source_url="https://example.com",
        )


def test_candidate_name_normalization_uses_authoritative_rule():
    assert normalize_candidate_name(" Jane Smith! ") == "jane smith"
    valid = DiscoveryCandidate(**{**candidate().model_dump(), "name": " Jane Smith! ", "normalized_name": "jane smith"})
    assert valid.normalized_name == normalize_candidate_name(valid.name)
    with pytest.raises(ValidationError):
        DiscoveryCandidate(**{**candidate().model_dump(), "name": "Jane Smith", "normalized_name": "john doe"})
    with pytest.raises(ValidationError):
        DiscoveryCandidate(**{**candidate().model_dump(), "name": "!!!", "normalized_name": ""})


def test_discovery_method_reuses_authoritative_vocabulary():
    for method in DISCOVERY_METHODS:
        assert DiscoveryCandidate(**{**candidate().model_dump(), "discovery_method": method}).discovery_method == method
    with pytest.raises(ValidationError):
        DiscoveryCandidate(**{**candidate().model_dump(), "discovery_method": "autonomous_browser_agent"})
    with pytest.raises(ValidationError):
        DiscoveryCandidate(**{**candidate().model_dump(), "discovery_method": "website"})


@pytest.mark.parametrize("value", [
    "Bearer opaque-secret",
    "Basic opaque-secret",
    "api_key=opaque-secret",
    "access_token=opaque-secret",
    "password: opaque-secret",
    "secret=opaque-secret",
    "https://user:opaque-secret@example.com/page",
    "https://example.com/?access_token=opaque-secret",
    "https://example.com/#api_key=opaque-secret",
    [{"metadata": ["token=opaque-secret"]}],
])
def test_credential_shaped_values_are_rejected_without_echoing_secret(value):
    with pytest.raises(ValidationError) as exc:
        request(provider_config_ref=value)
    assert "opaque-secret" not in str(exc.value)


def test_secret_detection_avoids_false_positives():
    safe_source = source()
    assert len(safe_source.content_hash) == 64
    assert safe_source.provider_request_id == "provider-1"
    assert request(correlation_id="corr-123").correlation_id == "corr-123"
    assert request(company_website="https://example.com/public").company_website == "https://example.com/public"
    assert DiscoverySource(**{**safe_source.model_dump(), "extracted_text": "A token is mentioned in ordinary prose."})


def test_candidate_rejects_unknown_role_and_outcome_invariants():
    with pytest.raises(ValidationError):
        DiscoveryCandidate(**{**candidate().model_dump(), "role_type": "free_form"})
    with pytest.raises(ValidationError):
        outcome(DiscoveryOutcomeStatus.NO_RESULT, no_result_reason=None)
    with pytest.raises(ValidationError):
        outcome(DiscoveryOutcomeStatus.RATE_LIMITED)


def test_outcome_is_bounded_by_request():
    with pytest.raises(ValueError):
        outcome().validate_for_request(request(result_limit=0))


def test_fake_provider_is_deterministic_and_captures_deep_copies():
    provider = FakeDiscoveryProvider(outcome())
    result = provider.execute(request())
    assert result.provider_request_id == "fake-0001"
    assert provider.requests[0].lead_id == 1
    result.candidates.clear()
    assert provider.execute(request()).candidates


def test_fake_provider_supports_no_result_without_side_effects():
    provider = FakeDiscoveryProvider(outcome(DiscoveryOutcomeStatus.NO_RESULT, no_result_reason="no visible people"))
    result = provider.execute(request())
    assert result.status == DiscoveryOutcomeStatus.NO_RESULT
    assert provider.requests[0].company_name == "Example Agency"


def test_outcome_rejects_completion_before_start_and_accepts_equal_timestamps():
    with pytest.raises(ValidationError, match="completed_at cannot precede started_at") as exc:
        outcome(completed_at=NOW - timedelta(seconds=1))
    assert "2026" not in str(exc.value)

    equal = outcome(completed_at=NOW)
    assert equal.started_at == NOW
    assert equal.completed_at == NOW


def test_cancelled_outcome_is_bounded_and_cannot_contain_candidates():
    cancelled = outcome(DiscoveryOutcomeStatus.CANCELLED, sources=[], candidates=[])
    assert cancelled.status is DiscoveryOutcomeStatus.CANCELLED
    assert not cancelled.candidates
    assert cancelled.retry_after is None
    assert cancelled.provider_name == "fake"

    with pytest.raises(ValidationError, match="cancelled outcome cannot contain candidates"):
        outcome(DiscoveryOutcomeStatus.CANCELLED, candidates=[candidate()])


def test_retryable_rate_limited_and_permanent_errors_remain_distinct():
    retryable = outcome(DiscoveryOutcomeStatus.RETRYABLE_ERROR, sources=[], candidates=[], safe_error_code="temporary")
    assert retryable.status is DiscoveryOutcomeStatus.RETRYABLE_ERROR

    rate_limited = outcome(
        DiscoveryOutcomeStatus.RATE_LIMITED,
        sources=[],
        candidates=[],
        retry_after=NOW + timedelta(seconds=30),
    )
    assert rate_limited.status is DiscoveryOutcomeStatus.RATE_LIMITED
    assert rate_limited.retry_after is not None

    permanent = outcome(DiscoveryOutcomeStatus.PERMANENT_ERROR, sources=[], candidates=[], safe_error_code="invalid")
    assert permanent.status is DiscoveryOutcomeStatus.PERMANENT_ERROR
    assert permanent.retry_after is None

    with pytest.raises(ValidationError, match="permanent_error cannot carry retry metadata"):
        outcome(
            DiscoveryOutcomeStatus.PERMANENT_ERROR,
            sources=[],
            candidates=[],
            retry_after=NOW + timedelta(seconds=30),
        )
    with pytest.raises(ValidationError, match="succeeded outcome requires a candidate"):
        outcome(DiscoveryOutcomeStatus.SUCCEEDED, sources=[], candidates=[])


def test_rate_limited_requires_retry_metadata():
    with pytest.raises(ValidationError, match="rate_limited outcome requires retry_after"):
        outcome(DiscoveryOutcomeStatus.RATE_LIMITED, sources=[], candidates=[])


def test_fake_provider_has_no_network_database_or_persistence_side_effects(monkeypatch):
    class UnexpectedSideEffect(RuntimeError):
        pass

    def fail_network(*args, **kwargs):
        raise UnexpectedSideEffect("network access")

    def fail_database(*args, **kwargs):
        raise UnexpectedSideEffect("database access")

    monkeypatch.setattr(socket, "socket", fail_network)
    monkeypatch.setattr(socket, "create_connection", fail_network)
    monkeypatch.setattr(sqlite3, "connect", fail_database)

    provider = FakeDiscoveryProvider(outcome())
    result = provider.execute(request())

    assert result.status is DiscoveryOutcomeStatus.SUCCEEDED
    assert result.candidates
    assert not hasattr(provider, "persist")
    assert not hasattr(provider, "create_canonical_person")
