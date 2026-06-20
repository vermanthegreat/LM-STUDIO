"""Add people.legacy_person_id for repeatable SQLite migration."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003_person_legacy_person_id"
down_revision: Union[str, None] = "002_task_created_by_command_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UNIQUE_NAME = "uq_people_legacy_person_id"


def _people_table_exists(inspector: sa.Inspector) -> bool:
    return "people" in inspector.get_table_names()


def _column_names(inspector: sa.Inspector) -> set[str]:
    return {column["name"] for column in inspector.get_columns("people")}


def _has_unique_on_legacy_person_id(inspector: sa.Inspector) -> bool:
    for constraint in inspector.get_unique_constraints("people"):
        if constraint.get("column_names") == ["legacy_person_id"]:
            return True
    for index in inspector.get_indexes("people"):
        if index.get("unique") and index.get("column_names") == ["legacy_person_id"]:
            return True
    return False


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _people_table_exists(inspector):
        return

    columns = _column_names(inspector)
    if "legacy_person_id" not in columns:
        op.add_column("people", sa.Column("legacy_person_id", sa.Integer(), nullable=True))

    inspector = sa.inspect(bind)
    if not _has_unique_on_legacy_person_id(inspector):
        op.create_unique_constraint(
            _UNIQUE_NAME,
            "people",
            ["legacy_person_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _people_table_exists(inspector):
        return

    if _has_unique_on_legacy_person_id(inspector):
        op.drop_constraint(_UNIQUE_NAME, "people", type_="unique")

    columns = _column_names(inspector)
    if "legacy_person_id" in columns:
        op.drop_column("people", "legacy_person_id")
