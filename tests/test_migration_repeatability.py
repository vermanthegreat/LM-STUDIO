"""Repeatable SQLite-to-PostgreSQL migration tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import db
import pytest
from persistence.models import Base, Interaction, Organization, Person, Task
from persistence.session import get_engine, reset_cached_engines
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from tests.pg_support import reset_public_schema, run_alembic_upgrade

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.migrate_sqlite_to_postgres import migrate

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")


pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is not configured",
)


def _seed_sqlite(db_path: Path) -> dict[str, int]:
    db.init_db(db_path)
    lead, _ = db.upsert_lead(
        {
            "company_name": "Repeatable Migration Co",
            "company_email": "hello@repeatable.example",
            "fit_score": 70,
        },
        db_path=db_path,
    )
    person = db.add_person(
        lead["id"],
        {"name": "Jane Doe", "title": "CEO", "email": "jane@repeatable.example"},
        db_path=db_path,
    )
    interaction = db.add_interaction(
        lead["id"],
        {
            "type": "email",
            "subject": "Intro",
            "summary": "Initial outreach",
            "reply_needed": 1,
        },
        db_path=db_path,
    )
    task = db.add_task(
        lead["id"],
        {"title": "Follow up", "status": "open", "priority": "normal"},
        db_path=db_path,
    )
    return {
        "lead_id": lead["id"],
        "person_id": person["id"],
        "interaction_id": interaction["id"],
        "task_id": task["id"],
    }


def _table_counts(database_url: str) -> dict[str, int]:
    engine = get_engine(database_url)
    with Session(bind=engine) as session:
        return {
            "organizations": session.scalar(select(func.count()).select_from(Organization)) or 0,
            "people": session.scalar(select(func.count()).select_from(Person)) or 0,
            "interactions": session.scalar(select(func.count()).select_from(Interaction)) or 0,
            "tasks": session.scalar(select(func.count()).select_from(Task)) or 0,
        }


def test_migration_is_repeatable_for_people_interactions_tasks(tmp_path):
    sqlite_path = tmp_path / "legacy.db"
    ids = _seed_sqlite(sqlite_path)

    reset_public_schema(TEST_DATABASE_URL)
    run_alembic_upgrade(TEST_DATABASE_URL, "head")

    first = migrate(sqlite_path, TEST_DATABASE_URL)
    assert first.migrated_organizations == 1
    assert first.migrated_people == 1
    assert first.migrated_interactions == 1
    assert first.migrated_tasks == 1

    counts_after_first = _table_counts(TEST_DATABASE_URL)
    assert counts_after_first == {
        "organizations": 1,
        "people": 1,
        "interactions": 1,
        "tasks": 1,
    }

    second = migrate(sqlite_path, TEST_DATABASE_URL)
    assert second.migrated_organizations == 0
    assert second.migrated_people == 0
    assert second.migrated_interactions == 0
    assert second.migrated_tasks == 0
    assert f"organization:legacy_lead_id:{ids['lead_id']}" in second.conflicts
    assert f"person:legacy_person_id:{ids['person_id']}" in second.conflicts
    assert f"interaction:legacy_interaction_id:{ids['interaction_id']}" in second.conflicts
    assert f"task:legacy_task_id:{ids['task_id']}" in second.conflicts

    counts_after_second = _table_counts(TEST_DATABASE_URL)
    assert counts_after_second == counts_after_first

    engine = get_engine(TEST_DATABASE_URL)
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(text(f'TRUNCATE TABLE "{table.name}" RESTART IDENTITY CASCADE'))
    reset_cached_engines()
