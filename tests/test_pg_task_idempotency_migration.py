"""PostgreSQL Alembic migration and task idempotency tests."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from persistence.models import Base
from persistence.session import get_engine, init_schema, reset_cached_engines
from repositories.postgres_store import PostgresContactStore
from sqlalchemy import inspect, text

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")


pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is not configured",
)


def _alembic_config(database_url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    os.environ["DATABASE_URL"] = database_url
    return cfg


def _reset_public_schema(database_url: str) -> None:
    engine = get_engine(database_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    reset_cached_engines()


def _run_alembic_upgrade(database_url: str, revision: str = "head") -> None:
    command.upgrade(_alembic_config(database_url), revision)


def _tasks_has_unique_command_id(database_url: str) -> bool:
    engine = get_engine(database_url)
    inspector = inspect(engine)
    if "tasks" not in inspector.get_table_names():
        return False
    columns = {column["name"] for column in inspector.get_columns("tasks")}
    if "created_by_command_id" not in columns:
        return False
    for constraint in inspector.get_unique_constraints("tasks"):
        if constraint.get("column_names") == ["created_by_command_id"]:
            return True
    for index in inspector.get_indexes("tasks"):
        if index.get("unique") and index.get("column_names") == ["created_by_command_id"]:
            return True
    return False


@pytest.fixture()
def migrated_pg_store():
    _reset_public_schema(TEST_DATABASE_URL)
    init_schema(TEST_DATABASE_URL)
    _run_alembic_upgrade(TEST_DATABASE_URL, "head")
    assert _tasks_has_unique_command_id(TEST_DATABASE_URL)
    store = PostgresContactStore(TEST_DATABASE_URL)
    yield store
    engine = get_engine(TEST_DATABASE_URL)
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(text(f'TRUNCATE TABLE "{table.name}" RESTART IDENTITY CASCADE'))
    reset_cached_engines()


def test_alembic_upgrade_head_succeeds_on_disposable_database():
    _reset_public_schema(TEST_DATABASE_URL)
    init_schema(TEST_DATABASE_URL)
    _run_alembic_upgrade(TEST_DATABASE_URL, "head")
    assert _tasks_has_unique_command_id(TEST_DATABASE_URL)


def test_migration_adds_created_by_command_id_on_legacy_tasks_table():
    _reset_public_schema(TEST_DATABASE_URL)
    init_schema(TEST_DATABASE_URL)

    engine = get_engine(TEST_DATABASE_URL)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE tasks DROP COLUMN IF EXISTS created_by_command_id"))

    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("tasks")}
    assert "created_by_command_id" not in columns

    _run_alembic_upgrade(TEST_DATABASE_URL, "head")
    assert _tasks_has_unique_command_id(TEST_DATABASE_URL)


def test_pg_add_task_is_idempotent_by_created_by_command_id(migrated_pg_store):
    store = migrated_pg_store
    lead, _ = store.upsert_lead({"company_name": "Idempotent Task Co", "fit_score": 50})
    command_id = uuid4()
    payload = {
        "title": "Follow up once",
        "status": "open",
        "created_by_command_id": str(command_id),
    }

    first = store.add_task(lead["id"], payload)
    second = store.add_task(lead["id"], payload)

    assert first["id"] == second["id"]
    assert first["title"] == "Follow up once"

    detail = store.get_lead(lead["id"])
    assert detail is not None
    assert len(detail["tasks"]) == 1
