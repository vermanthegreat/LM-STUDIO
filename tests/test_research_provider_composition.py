from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import db
import pytest

from discovery_models import DiscoveryRequest
from repositories.sqlite_store import SqliteContactStore
from research_job_models import ResearchJobStatus
from services.company_website_discovery_provider import CompanyWebsiteDiscoveryProvider
from services.company_website_http_transport import CompanyWebsiteTransportError, HardenedCompanyWebsiteHttpTransport
from services.research_job_runner import DiscoveryOutcomeMaterializerPort, ResearchJobRunner
from services.research_provider_composition import (
    CompanyWebsiteTransportDependencies,
    SQLiteDiscoveryOutcomeMaterializerAdapter,
    build_research_job_runner,
)
from tests.test_company_website_http_transport import FakeClock, FakeFactory, FakeResponse, FakeResolver
from tests.test_research_job_persistence import create_job, setup_db


NOW = datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc)
HOME = b"<html><head><title>Example Agency</title></head><body><h1>Example Agency</h1><a href='/team'>Team</a></body></html>"
TEAM = b"<html><body><section><h2>Jane Doe</h2><p>Chief Executive Officer</p><a href='mailto:jane@example.com'>Email</a></section></body></html>"
NO_RESULT = b"<html><head><title>Example Agency</title></head><body><h1>Example Agency</h1></body></html>"


def _request(lead_id: int, *, max_pages: int = 2, job_id: str = "website-job") -> DiscoveryRequest:
    return DiscoveryRequest(
        job_id=job_id,
        lead_id=lead_id,
        company_name="Example Agency",
        normalized_domain="example.com",
        company_website="https://example.com",
        target_roles=["economic_buyer"],
        result_limit=3,
        approved_source_types=["website"],
        requested_at=NOW,
        requester_identity="operator",
        correlation_id=job_id + "-correlation",
        provider_config_ref="website-test",
        max_pages=max_pages,
        max_requests=3,
        timeout_seconds=10,
    )


def _dependencies(responses: list[FakeResponse], *, resolver=None):
    resolver = resolver or FakeResolver({"example.com": ("93.184.216.34",)})
    factory = FakeFactory(responses)
    return CompanyWebsiteTransportDependencies(resolver=resolver, connection_factory=factory, clock=FakeClock()), resolver, factory


def _enqueue(path, lead_id: int, *, max_pages: int = 2, job_id: str = "website-job"):
    store = SqliteContactStore(path)
    job = store.enqueue_research_job(create_job(request=_request(lead_id, max_pages=max_pages, job_id=job_id), adapter_key="company_website"))
    return store, job


def _canonical_lead_state(path, lead_id: int) -> tuple[object, ...]:
    with db.get_conn(path) as conn:
        row = conn.execute(
            """SELECT company_name, company_email, company_phone, website, domain,
                      fit_score, status, enrichment_status, partner_tier, description
               FROM leads WHERE id = ?""",
            (lead_id,),
        ).fetchone()
    return tuple(row)


def test_composition_is_explicit_inert_and_nominal():
    resolver = FakeResolver({"example.com": ("93.184.216.34",)})
    factory = FakeFactory([])
    repository = SimpleNamespace()
    runner = build_research_job_runner(
        repository=repository,
        worker_id="composition-test",
        network_dependencies=CompanyWebsiteTransportDependencies(resolver=resolver, connection_factory=factory, clock=FakeClock()),
    )

    assert isinstance(runner, ResearchJobRunner)
    assert tuple(runner.providers) == ("company_website",)
    provider = runner.providers["company_website"]
    assert isinstance(provider, CompanyWebsiteDiscoveryProvider)
    assert isinstance(provider._fetcher, HardenedCompanyWebsiteHttpTransport)
    assert isinstance(runner._materializer, DiscoveryOutcomeMaterializerPort)
    assert resolver.calls == []
    assert factory.calls == []


def test_end_to_end_materializes_governed_candidates_without_canonical_mutation(tmp_path):
    path, lead, *_ = setup_db(tmp_path)
    store, job = _enqueue(path, lead["id"])
    dependencies, resolver, factory = _dependencies([
        FakeResponse(200, HOME, {"Content-Type": "text/html"}),
        FakeResponse(200, TEAM, {"Content-Type": "text/html"}),
    ])
    before_lead = _canonical_lead_state(path, lead["id"])
    runner = build_research_job_runner(repository=store, worker_id="one-shot", network_dependencies=dependencies)

    result = runner.run_next(worker_id="one-shot")

    assert result is not None and result.final_status is ResearchJobStatus.SUCCEEDED
    assert result.materialized_source_count == 2
    assert result.materialized_person_candidate_count == 1
    assert result.materialized_contact_candidate_count == 1
    assert len(resolver.calls) == 2
    assert len(factory.connections) == 2
    assert _canonical_lead_state(path, lead["id"]) == before_lead
    with db.get_conn(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM people WHERE lead_id = ?", (lead["id"],)).fetchone()[0] == 1
        # setup_db seeds one candidate; this run materializes one additional
        # governed candidate without applying either candidate canonically.
        assert conn.execute("SELECT COUNT(*) FROM person_candidates WHERE lead_id = ?", (lead["id"],)).fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM contact_method_candidates WHERE lead_id = ?", (lead["id"],)).fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM research_job_materializations WHERE research_job_id = ?", (job.id,)).fetchone()[0] == 1
    assert not hasattr(result, "lease_token")


def test_no_result_materializes_source_without_candidates(tmp_path):
    path, lead, *_ = setup_db(tmp_path)
    store, _ = _enqueue(path, lead["id"], max_pages=1, job_id="no-result")
    dependencies, _, _ = _dependencies([FakeResponse(200, NO_RESULT, {"Content-Type": "text/html"})])
    with db.get_conn(path) as conn:
        before_people = conn.execute("SELECT COUNT(*) FROM people WHERE lead_id = ?", (lead["id"],)).fetchone()[0]
        before_person_candidates = conn.execute("SELECT COUNT(*) FROM person_candidates WHERE lead_id = ?", (lead["id"],)).fetchone()[0]
        before_contact_candidates = conn.execute("SELECT COUNT(*) FROM contact_method_candidates WHERE lead_id = ?", (lead["id"],)).fetchone()[0]
    runner = build_research_job_runner(repository=store, worker_id="no-result", network_dependencies=dependencies)

    result = runner.run_next(worker_id="no-result")

    assert result is not None and result.final_status is ResearchJobStatus.NO_RESULT
    assert result.materialized_source_count == 1
    assert result.materialized_person_candidate_count == 0
    with db.get_conn(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM person_candidates WHERE lead_id = ?", (lead["id"],)).fetchone()[0] == before_person_candidates
        assert conn.execute("SELECT COUNT(*) FROM contact_method_candidates WHERE lead_id = ?", (lead["id"],)).fetchone()[0] == before_contact_candidates
        assert conn.execute("SELECT COUNT(*) FROM people WHERE lead_id = ?", (lead["id"],)).fetchone()[0] == before_people


def test_transport_failure_does_not_materialize_or_expose_raw_error(tmp_path):
    path, lead, *_ = setup_db(tmp_path)
    store, job = _enqueue(path, lead["id"], job_id="failure")
    with db.get_conn(path) as conn:
        before_sources = conn.execute("SELECT COUNT(*) FROM raw_sources WHERE lead_id = ?", (lead["id"],)).fetchone()[0]
        before_person_candidates = conn.execute("SELECT COUNT(*) FROM person_candidates WHERE lead_id = ?", (lead["id"],)).fetchone()[0]
    resolver = FakeResolver({"example.com": ("93.184.216.34",)})
    factory = FakeFactory([FakeResponse(200, HOME, {"Content-Type": "text/html"})])
    factory.responses = []
    def fail_connect(**kwargs):
        factory.calls.append(kwargs)
        raise CompanyWebsiteTransportError("company_website_connect_failed")

    factory.connect = fail_connect
    runner = build_research_job_runner(
        repository=store,
        worker_id="failure",
        network_dependencies=CompanyWebsiteTransportDependencies(resolver=resolver, connection_factory=factory, clock=FakeClock()),
    )

    result = runner.run_next(worker_id="failure")

    assert result is not None and result.final_status is ResearchJobStatus.RETRY_WAIT
    record = store.get_research_job(job.id)
    assert record.safe_error_code == "company_website_fetch_failed"
    assert json.loads(record.result_summary_json)["underlying_result_code"] == "company_website_connect_failed"
    assert "private" not in json.dumps(record.model_dump(mode="json"))
    with db.get_conn(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM raw_sources WHERE lead_id = ?", (lead["id"],)).fetchone()[0] == before_sources
        assert conn.execute("SELECT COUNT(*) FROM person_candidates WHERE lead_id = ?", (lead["id"],)).fetchone()[0] == before_person_candidates
    assert len(factory.calls) <= 2


def test_one_shot_command_requires_confirmation_and_bounds_output(monkeypatch, capsys):
    from scripts import run_company_website_research_once as command

    assert command.main([]) == 2
    assert "--run-one" in capsys.readouterr().out

    calls = {"runs": 0}

    class Store:
        def init_db(self):
            return None

        def list_research_jobs(self, *, status, limit):
            return [SimpleNamespace(adapter_key="company_website")]

    class Runner:
        def run_next(self, *, worker_id):
            calls["runs"] += 1
            return SimpleNamespace(
                terminal=True,
                final_status=ResearchJobStatus.SUCCEEDED,
                materialized_source_count=1,
                materialized_person_candidate_count=1,
                materialized_contact_candidate_count=1,
                lease_token="secret-lease",
                materialization_receipt_id=99,
            )

    monkeypatch.setattr(command.AppConfig, "from_env", classmethod(lambda cls: object()))
    monkeypatch.setattr(command, "get_contact_store", lambda config: Store())
    monkeypatch.setattr(command, "build_research_job_runner", lambda **kwargs: Runner())

    assert command.main(["--run-one"]) == 0
    output = capsys.readouterr().out
    assert calls["runs"] == 1
    assert "status=succeeded" in output
    assert "lease" not in output and "99" not in output
