"""Add tasks.created_by_command_id for write-proposal idempotency."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "002_task_created_by_command_id"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UNIQUE_NAME = "uq_tasks_created_by_command_id"


def _tasks_table_exists(inspector: sa.Inspector) -> bool:
    return "tasks" in inspector.get_table_names()


def _column_names(inspector: sa.Inspector) -> set[str]:
    return {column["name"] for column in inspector.get_columns("tasks")}


def _has_unique_on_command_id(inspector: sa.Inspector) -> bool:
    for constraint in inspector.get_unique_constraints("tasks"):
        if constraint.get("column_names") == ["created_by_command_id"]:
            return True
    for index in inspector.get_indexes("tasks"):
        if index.get("unique") and index.get("column_names") == ["created_by_command_id"]:
            return True
    return False


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _tasks_table_exists(inspector):
        return

    columns = _column_names(inspector)
    if "created_by_command_id" not in columns:
        op.add_column(
            "tasks",
            sa.Column("created_by_command_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
        inspector = sa.inspect(bind)

    if not _has_unique_on_command_id(inspector):
        op.create_unique_constraint(
            _UNIQUE_NAME,
            "tasks",
            ["created_by_command_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _tasks_table_exists(inspector):
        return

    if _has_unique_on_command_id(inspector):
        op.drop_constraint(_UNIQUE_NAME, "tasks", type_="unique")

    columns = _column_names(inspector)
    if "created_by_command_id" in columns:
        op.drop_column("tasks", "created_by_command_id")
