from __future__ import annotations

from datetime import datetime, timedelta, timezone

import db
import pytest

from discovery_models import DiscoveryOutcomeStatus, DiscoveryRequest
from providers.discovery_base import DiscoveryProvider
from repositories.sqlite_store import SqliteContactStore
from services.company_website_discovery_provider import (
    CompanyWebsiteDiscoveryProvider,
    CompanyWebsiteDiscoveryProviderError,
    CompanyWebsiteFetchPort,
    CompanyWebsiteFetchResponse,
)
from services.discovery_outcome_materialization import materialize_discovery_outcome
from tests.test_research_job_lifecycle import running_job


NOW = datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc)


def request(**overrides) -> DiscoveryRequest:
    data = {
        "job_id": "website-job",
        "lead_id": 1,
        "company_name": "Example Agency",
        "normalized_domain": "example.com",
        "company_website": "https://example.com",
        "target_roles": ["economic_buyer"],
        "result_limit": 3,
        "approved_source_types": ["website"],
        "requested_at": NOW,
        "requester_identity": "operator",
        "correlation_id": "website-correlation",
        "provider_config_ref": "website-test",
        "max_pages": 3,
        "max_requests": 3,
        "timeout_seconds": 10,
    }
    data.update(overrides)
    return DiscoveryRequest(**data)


class MemoryFetcher(CompanyWebsiteFetchPort):
    def __init__(self, pages: dict[str, CompanyWebsiteFetchResponse | Exception]):
        self.pages = pages
        self.calls: list[tuple[str, int, int]] = []

    def fetch(self, *, url: str, timeout_seconds: int, max_response_bytes: int) -> CompanyWebsiteFetchResponse:
        self.calls.append((url, timeout_seconds, max_response_bytes))
        value = self.pages.get(url, self.pages.get(url.rstrip("/")))
        if value is None:
            raise KeyError(url)
        if isinstance(value, Exception):
            raise value
        return value


def response(url: str, body: str, *, status: int = 200, content_type: str = "text/html", final_url: str | None = None, retry_after=None):
    return CompanyWebsiteFetchResponse(
        requested_url=url,
        final_url=final_url or url,
        http_status=status,
        content_type=content_type,
        body=body.encode("utf-8"),
        retry_after=retry_after,
    )


def provider(pages: dict[str, CompanyWebsiteFetchResponse | Exception]) -> tuple[CompanyWebsiteDiscoveryProvider, MemoryFetcher]:
    fetcher = MemoryFetcher(pages)
    return CompanyWebsiteDiscoveryProvider(fetcher), fetcher


HOME = """
<html><head><title>Example Agency</title></head><body>
<h1>Example Agency</h1><a href="/team">Our Team</a><a href="/blog">Blog</a>
</body></html>
"""
TEAM = """
<html><head><title>Team</title></head><body><section><h2>Jane Doe</h2>
<p>Chief Executive Officer</p><a href="mailto:jane@example.com?subject=hello">Email</a>
<a href="https://www.linkedin.com/in/jane-doe">Profile</a></section></body></html>
"""


def test_fetch_port_is_nominal_and_constructor_does_not_invoke_it():
    class ValidFetcher(CompanyWebsiteFetchPort):
        def __init__(self):
            self.calls = 0

        def fetch(self, *, url, timeout_seconds, max_response_bytes):
            self.calls += 1
            raise AssertionError

    valid = ValidFetcher()
    instance = CompanyWebsiteDiscoveryProvider(valid)
    assert isinstance(instance, DiscoveryProvider)
    assert valid.calls == 0
    for dependency in (None, lambda **kwargs: None, object()):
        with pytest.raises(CompanyWebsiteDiscoveryProviderError) as error:
            CompanyWebsiteDiscoveryProvider(dependency)
        assert error.value.code == "invalid_company_website_fetch_dependency"
        assert str(error.value) == "company website fetch dependency is invalid"


def test_response_is_immutable_bounded_and_forbids_invalid_values():
    value = response("https://example.com", "<html></html>", content_type="text/html; charset=utf-8")
    assert value.content_type == "text/html"
    with pytest.raises(ValueError):
        value.body = b"changed"
    with pytest.raises(ValueError):
        response("https://user:secret@example.com", "x")
    with pytest.raises(ValueError):
        CompanyWebsiteFetchResponse(
            requested_url="https://example.com", final_url="https://example.com", http_status=200,
            content_type="text/html", body=b"x", extra="forbidden",
        )


@pytest.mark.parametrize("url", [
    "https://user:pass@example.com",
    "https://127.0.0.1",
    "https://localhost",
    "https://company.local",
    "https://company.internal",
    "https://example.com/?redirect=other",
    "https://example.com/../team",
    "https://example.com:8443",
])
def test_invalid_website_boundaries_fail_closed(url):
    site, fetcher = provider({"https://example.com": response("https://example.com", HOME)})
    invalid_request = request().model_copy(update={"company_website": url})
    result = site.execute(invalid_request)
    assert result.status is DiscoveryOutcomeStatus.PERMANENT_ERROR
    assert fetcher.calls == []


def test_homepage_and_http_website_are_accepted_with_normalized_urls():
    site, fetcher = provider({"http://example.com/": response("http://example.com/", HOME)})
    result = site.execute(request(company_website="HTTP://Example.com///"))
    assert result.status is DiscoveryOutcomeStatus.NO_RESULT
    assert fetcher.calls[0][0] == "http://example.com/"


def test_allowed_host_is_exact_or_www_but_not_arbitrary_subdomain():
    pages = {
        "https://example.com": response("https://example.com", HOME.replace('href="/team"', 'href="https://www.example.com/team"')),
        "https://www.example.com/team": response("https://www.example.com/team", TEAM),
    }
    site, fetcher = provider(pages)
    result = site.execute(request(company_website="https://example.com"))
    assert result.status is DiscoveryOutcomeStatus.PARTIAL
    assert "https://www.example.com/team" in [call[0] for call in fetcher.calls]

    offhost = response("https://example.com", HOME, final_url="https://evil.example/team")
    site, fetcher = provider({"https://example.com": offhost})
    result = site.execute(request())
    assert result.status is DiscoveryOutcomeStatus.PERMANENT_ERROR
    assert fetcher.calls == [("https://example.com/", 10, 1024 * 1024)]


def test_homepage_links_precede_conventional_paths_and_ineligible_links_are_skipped():
    pages = {
        "https://example.com": response("https://example.com", HOME),
        "https://example.com/team": response("https://example.com/team", TEAM),
        "https://example.com/about": response("https://example.com/about", "<title>About</title>"),
    }
    site, fetcher = provider(pages)
    result = site.execute(request(max_requests=3, max_pages=3))
    assert result.status is DiscoveryOutcomeStatus.SUCCEEDED
    assert [call[0] for call in fetcher.calls] == [
        "https://example.com/", "https://example.com/team", "https://example.com/about",
    ]
    assert len(result.sources) == 3


def test_static_html_extracts_explicit_card_contacts_and_source_evidence():
    site, _ = provider({"https://example.com": response("https://example.com", HOME), "https://example.com/team": response("https://example.com/team", TEAM)})
    result = site.execute(request(max_requests=2, max_pages=2))
    assert result.status is DiscoveryOutcomeStatus.SUCCEEDED
    candidate = result.candidates[0]
    assert candidate.name == "Jane Doe"
    assert candidate.role_type == "economic_buyer"
    assert candidate.raw_evidence_reference == "https://example.com/team"
    assert candidate.explicit_contacts[0].value == "jane@example.com"
    assert candidate.explicit_contacts[0].verification_status == "unverified"
    assert candidate.explicit_contacts[1].kind == "linkedin"
    assert result.sources[1].content_hash != result.sources[0].content_hash


def test_json_ld_person_is_preferred_and_microdata_is_supported():
    home = """
    <script type="application/ld+json">{"@type":"Person","name":"Jane Doe","jobTitle":"CEO","email":"jane@example.com"}</script>
    <div itemscope itemtype="https://schema.org/Person"><span itemprop="name">John Smith</span><span itemprop="jobTitle">Founder</span></div>
    """
    site, _ = provider({"https://example.com": response("https://example.com", home)})
    result = site.execute(request(max_requests=1, max_pages=1, target_roles=["economic_buyer"]))
    assert result.status is DiscoveryOutcomeStatus.SUCCEEDED
    assert [candidate.name for candidate in result.candidates] == ["Jane Doe", "John Smith"]
    assert result.candidates[0].confidence > result.candidates[1].confidence


def test_json_ld_graph_and_malformed_blocks_are_bounded():
    home = """
    <script type="application/ld+json">{malformed</script>
    <script type="application/ld+json">{"@graph":[{"@type":"Person","name":"Jane Doe","jobTitle":"CEO"}]}</script>
    """
    site, _ = provider({"https://example.com": response("https://example.com", home)})
    result = site.execute(request(max_requests=1, max_pages=1))
    assert result.status is DiscoveryOutcomeStatus.SUCCEEDED
    assert result.candidates[0].name == "Jane Doe"


def test_roles_are_exactly_filtered_and_broad_substrings_do_not_match():
    home = """
    <script type="application/ld+json">[
      {"@type":"Person","name":"Jane Doe","jobTitle":"Founder and CEO"},
      {"@type":"Person","name":"John Smith","jobTitle":"CEO Advisor"},
      {"@type":"Person","name":"Alex Jones","jobTitle":"Head of Commerce"}
    ]</script>
    """
    site, _ = provider({"https://example.com": response("https://example.com", home)})
    result = site.execute(request(max_requests=1, max_pages=1, target_roles=["operational_owner"]))
    assert [candidate.name for candidate in result.candidates] == ["Alex Jones"]
    assert result.candidates[0].role_type == "operational_owner"


def test_result_limit_is_deterministic_and_uses_partial():
    home = """<script type="application/ld+json">[
    {"@type":"Person","name":"A One","jobTitle":"CEO"},
    {"@type":"Person","name":"B Two","jobTitle":"Founder"},
    {"@type":"Person","name":"C Three","jobTitle":"Owner"}
    ]</script>"""
    site, _ = provider({"https://example.com": response("https://example.com", home)})
    result = site.execute(request(max_requests=1, max_pages=1, result_limit=2))
    assert result.status is DiscoveryOutcomeStatus.PARTIAL
    assert len(result.candidates) == 2
    assert "candidate_limit_reached" in result.warnings


def test_no_result_and_controlled_transport_statuses():
    site, _ = provider({"https://example.com": response("https://example.com", "<title>Company</title>")})
    result = site.execute(request(max_requests=1, max_pages=1))
    assert result.status is DiscoveryOutcomeStatus.NO_RESULT
    assert result.no_result_reason == "no_matching_company_people"

    retry, _ = provider({"https://example.com": response("https://example.com", "", status=500)})
    assert retry.execute(request(max_requests=1, max_pages=1)).status is DiscoveryOutcomeStatus.RETRYABLE_ERROR
    limited, _ = provider({"https://example.com": response("https://example.com", "", status=429, retry_after=NOW + timedelta(minutes=1))})
    assert limited.execute(request(max_requests=1, max_pages=1)).status is DiscoveryOutcomeStatus.RATE_LIMITED
    denied, _ = provider({"https://example.com": response("https://example.com", "", status=403)})
    assert denied.execute(request(max_requests=1, max_pages=1)).safe_error_code == "company_website_access_denied"


def test_secondary_failure_is_partial_and_does_not_discard_valid_candidates():
    site, fetcher = provider({
        "https://example.com": response("https://example.com", HOME),
        "https://example.com/team": response("https://example.com/team", TEAM),
        "https://example.com/about": RuntimeError("private transport secret"),
    })
    result = site.execute(request(max_requests=3, max_pages=3))
    assert result.status is DiscoveryOutcomeStatus.PARTIAL
    assert result.candidates
    assert "secondary_page_fetch_failed" in result.warnings
    assert "private transport" not in str(result)
    assert len(fetcher.calls) == 3


def test_non_html_is_skipped_and_bounds_are_honored():
    pages = {
        "https://example.com": response("https://example.com", HOME),
        "https://example.com/team": response("https://example.com/team", "pdf", content_type="application/pdf"),
        "https://example.com/about": response("https://example.com/about", "<title>About</title>"),
    }
    site, fetcher = provider(pages)
    result = site.execute(request(max_requests=2, max_pages=1))
    assert len(fetcher.calls) <= 2
    assert len(result.sources) <= 1
    assert "secondary_page_unsupported_content" in result.warnings or result.status is DiscoveryOutcomeStatus.NO_RESULT


def test_deterministic_serialization_and_no_external_profile_fetch():
    html = """<script type="application/ld+json">{"@type":"Person","name":"Jane Doe","jobTitle":"CEO","sameAs":["https://linkedin.com/in/jane"]}</script>"""
    first, fetcher = provider({"https://example.com": response("https://example.com", html)})
    second, _ = provider({"https://example.com": response("https://example.com", html)})
    a = first.execute(request(max_requests=1, max_pages=1))
    b = second.execute(request(max_requests=1, max_pages=1))
    assert a.model_dump(mode="json") == b.model_dump(mode="json")
    assert len(fetcher.calls) == 1
    assert a.candidates[0].profile_url == "https://linkedin.com/in/jane"


def test_provider_outcome_materializes_without_canonical_mutation(tmp_path):
    site, _ = provider({"https://example.com": response("https://example.com", TEAM)})
    value = site.execute(request(max_requests=1, max_pages=1))
    assert value.status is DiscoveryOutcomeStatus.SUCCEEDED
    path, store, running = running_job(tmp_path)
    with db.get_conn(path) as conn:
        before_people = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
        before_contact_candidates = conn.execute("SELECT COUNT(*) FROM contact_method_candidates").fetchone()[0]
    result = materialize_discovery_outcome(
        store,
        research_job_id=running.id,
        lease_token=running.lease_token,
        expected_version=running.version,
        outcome=value,
    )
    assert result.raw_source_ids and result.person_candidate_ids and result.contact_candidate_ids
    assert store.get_research_job(running.id).status.value == "running"
    with db.get_conn(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == before_people
        assert conn.execute("SELECT COUNT(*) FROM contact_method_candidates").fetchone()[0] == before_contact_candidates + len(result.contact_candidate_ids)


def test_provider_never_persists_or_calls_runner():
    site, fetcher = provider({"https://example.com": response("https://example.com", TEAM)})
    result = site.execute(request(max_requests=1, max_pages=1))
    assert result.sources and result.candidates
    assert len(fetcher.calls) == 1
    assert not hasattr(site, "repository")
