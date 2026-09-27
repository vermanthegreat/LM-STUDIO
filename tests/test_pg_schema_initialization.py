"""PostgreSQL schema must be created via Alembic, not runtime create_all."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from persistence.models import Base, Organization
from persistence.session import (
    PostgreSQLSchemaError,
    ensure_postgresql_schema,
    get_engine,
    init_schema,
    reset_cached_engines,
)
from repositories.postgres_store import PostgresContactStore
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session
from tests.pg_support import reset_public_schema, run_alembic_upgrade

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")


pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is not configured",
)


def test_alembic_upgrade_head_creates_schema():
    reset_public_schema(TEST_DATABASE_URL)
    run_alembic_upgrade(TEST_DATABASE_URL, "head")

    engine = get_engine(TEST_DATABASE_URL)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert "organizations" in tables
    assert "command_log" in tables
    assert "alembic_version" in tables


def test_init_schema_does_not_call_create_all_for_postgresql():
    reset_public_schema(TEST_DATABASE_URL)
    run_alembic_upgrade(TEST_DATABASE_URL, "head")
    reset_cached_engines()

    with patch.object(Base.metadata, "create_all") as create_all:
        init_schema(TEST_DATABASE_URL)
        create_all.assert_not_called()


def test_repository_operates_after_alembic_migration():
    reset_public_schema(TEST_DATABASE_URL)
    run_alembic_upgrade(TEST_DATABASE_URL, "head")
    reset_cached_engines()

    store = PostgresContactStore(TEST_DATABASE_URL)
    store.init_db()
    lead, is_new = store.upsert_lead(
        {"company_name": "Alembic Schema Co", "company_email": "hello@alembic.example"}
    )
    assert is_new is True
    assert lead["company_name"] == "Alembic Schema Co"

    engine = get_engine(TEST_DATABASE_URL)
    with Session(bind=engine) as session:
        count = session.scalar(select(Organization).where(Organization.name == "Alembic Schema Co"))
        assert count is not None


def test_missing_schema_is_not_created_by_runtime_init():
    reset_public_schema(TEST_DATABASE_URL)
    reset_cached_engines()

    with pytest.raises(PostgreSQLSchemaError, match="alembic upgrade head"):
        init_schema(TEST_DATABASE_URL)

    engine = get_engine(TEST_DATABASE_URL)
    inspector = inspect(engine)
    assert "organizations" not in inspector.get_table_names()

    with pytest.raises(PostgreSQLSchemaError, match="alembic upgrade head"):
        ensure_postgresql_schema(engine)


def test_postgres_store_init_db_fails_without_migrations():
    reset_public_schema(TEST_DATABASE_URL)
    reset_cached_engines()

    store = PostgresContactStore(TEST_DATABASE_URL)
    with pytest.raises(PostgreSQLSchemaError, match="alembic upgrade head"):
        store.init_db()


def test_init_schema_fails_when_required_columns_are_missing():
    reset_public_schema(TEST_DATABASE_URL)
    run_alembic_upgrade(TEST_DATABASE_URL, "head")
    reset_cached_engines()

    engine = get_engine(TEST_DATABASE_URL)
    with engine.begin() as conn:
        from sqlalchemy import text

        conn.execute(text("ALTER TABLE people DROP COLUMN IF EXISTS legacy_person_id"))

    with pytest.raises(PostgreSQLSchemaError, match="people.legacy_person_id"):
        init_schema(TEST_DATABASE_URL)

    reset_public_schema(TEST_DATABASE_URL)
    run_alembic_upgrade(TEST_DATABASE_URL, "head")
    reset_cached_engines()
