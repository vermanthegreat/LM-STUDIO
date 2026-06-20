"""Verified-email semantics for read tools and analytics."""

from __future__ import annotations

import os

import db
import pytest
from persistence.models import ContactMethod, Organization
from persistence.session import get_engine, init_schema, reset_cached_engines, session_scope
from repositories.postgres_store import PostgresContactStore
from repositories.sqlite_store import SqliteContactStore
from sqlalchemy import select
from tools.registry import build_default_registry

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")


def _sqlite_store(tmp_path):
    db_path = tmp_path / "verified.db"
    db.init_db(db_path)
    db.upsert_lead(
        {
            "company_name": "Has Email Co",
            "company_email": "hello@example.com",
            "fit_score": 80,
        },
        db_path=db_path,
    )
    db.upsert_lead({"company_name": "No Email Co", "fit_score": 40}, db_path=db_path)
    return SqliteContactStore(db_path)


def test_sqlite_missing_any_excludes_lead_with_company_email(tmp_path):
    store = _sqlite_store(tmp_path)
    registry = build_default_registry()
    result = registry.execute(
        store,
        "find_companies_missing_email",
        {"missing_definition": "any"},
    )
    assert result.record_count == 1
    assert result.records[0]["company_name"] == "No Email Co"


def test_sqlite_missing_verified_includes_lead_with_untracked_email(tmp_path):
    store = _sqlite_store(tmp_path)
    registry = build_default_registry()
    result = registry.execute(
        store,
        "find_companies_missing_email",
        {"missing_definition": "verified"},
    )
    assert result.record_count == 2
    company_names = {record["company_name"] for record in result.records}
    assert company_names == {"Has Email Co", "No Email Co"}
    assert "sqlite_backend_verification_not_tracked" in result.warnings


def test_sqlite_verified_email_coverage_is_zero(tmp_path):
    store = _sqlite_store(tmp_path)
    registry = build_default_registry()
    result = registry.execute(
        store,
        "calculate_pipeline_analytics",
        {"metric": "verified_email_coverage"},
    )
    assert result.records[0]["metric"] == "verified_email_coverage_percent"
    assert result.records[0]["value"] == 0.0
    summary = store.get_contact_summary()
    assert summary["companies"] == 2
    assert summary["with_verified_email"] == 0
    assert "sqlite_backend_verification_not_tracked" in result.warnings


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not configured")
def test_postgres_verified_email_missing_and_coverage():
    reset_cached_engines()
    init_schema(TEST_DATABASE_URL)
    store = PostgresContactStore(TEST_DATABASE_URL)
    registry = build_default_registry()

    verified_lead, _ = store.upsert_lead(
        {"company_name": "Verified Co", "company_email": "verified@example.com"}
    )
    store.upsert_lead(
        {"company_name": "Unverified Co", "company_email": "unverified@example.com"}
    )

    with session_scope(TEST_DATABASE_URL) as session:
        org = session.scalar(
            select(Organization).where(Organization.legacy_lead_id == verified_lead["id"])
        )
        assert org is not None
        method = session.scalar(
            select(ContactMethod).where(
                ContactMethod.organization_id == org.id,
                ContactMethod.kind == "email",
            )
        )
        assert method is not None
        method.verification_status = "verified"

    missing_verified = registry.execute(
        store,
        "find_companies_missing_email",
        {"missing_definition": "verified"},
    )
    missing_names = {record["company_name"] for record in missing_verified.records}
    assert "Verified Co" not in missing_names
    assert "Unverified Co" in missing_names

    coverage = registry.execute(
        store,
        "calculate_pipeline_analytics",
        {"metric": "verified_email_coverage"},
    )
    assert coverage.records[0]["value"] == 50.0
    summary = store.get_contact_summary()
    assert summary["companies"] == 2
    assert summary["with_verified_email"] == 1

    engine = get_engine(TEST_DATABASE_URL)
    with engine.begin() as conn:
        from persistence.models import Base
        from sqlalchemy import text

        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(text(f'TRUNCATE TABLE "{table.name}" RESTART IDENTITY CASCADE'))
    reset_cached_engines()
