"""PostgreSQL integration tests (require TEST_DATABASE_URL)."""

from __future__ import annotations

import os

import pytest
from persistence.models import Base
from persistence.session import get_engine, init_schema, reset_cached_engines
from repositories.postgres_store import PostgresContactStore
from sqlalchemy import text

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")


pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is not configured",
)


@pytest.fixture()
def pg_store():
    reset_cached_engines()
    init_schema(TEST_DATABASE_URL)
    store = PostgresContactStore(TEST_DATABASE_URL)
    yield store
    engine = get_engine(TEST_DATABASE_URL)
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(text(f'TRUNCATE TABLE "{table.name}" RESTART IDENTITY CASCADE'))
    reset_cached_engines()


def test_postgres_upsert_and_read_lead(pg_store):
    lead, is_new = pg_store.upsert_lead(
        {
            "company_name": "PG Test Co",
            "website": "https://pg-test.example",
            "company_email": "hello@pg-test.example",
            "fit_score": 80,
            "services": ["shopify"],
        }
    )
    assert is_new is True
    assert lead["company_name"] == "PG Test Co"
    assert lead["company_email"] == "hello@pg-test.example"

    loaded = pg_store.get_lead(lead["id"])
    assert loaded is not None
    assert loaded["fit_score"] == 80
    assert loaded["services"] == ["shopify"]


def test_postgres_extraction_proposal_and_contact_methods(pg_store):
    lead, _ = pg_store.upsert_lead({"company_name": "Extraction Co", "fit_score": 50})
    source = pg_store.create_raw_source(
        source_type="note",
        raw_text="Contact: Jane Doe <jane@extraction.example>",
        parsed_json={"company_name": "Extraction Co", "confidence": 0.8},
        extraction_status="needs_review",
        confidence=0.8,
        lead_id=lead["id"],
    )
    assert source["id"] is not None

    pg_store.add_person(
        lead["id"],
        {"name": "Jane Doe", "email": "jane@extraction.example", "title": "CEO"},
        raw_source_id=source["id"],
    )
    detail = pg_store.get_lead(lead["id"])
    assert detail is not None
    assert len(detail["people"]) == 1
    assert detail["people"][0]["email"] == "jane@extraction.example"


def test_postgres_contact_summary(pg_store):
    pg_store.upsert_lead({"company_name": "With Email", "company_email": "a@example.com"})
    pg_store.upsert_lead({"company_name": "Without Email"})
    summary = pg_store.get_contact_summary()
    assert summary["companies"] == 2
    assert summary["with_any_email"] == 1
    assert summary["without_email"] == 1
