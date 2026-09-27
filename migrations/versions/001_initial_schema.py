"""Initial PostgreSQL contact schema."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from persistence.models import Base

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _schema_exists(inspector: sa.Inspector) -> bool:
    return "organizations" in inspector.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _schema_exists(inspector):
        return
    Base.metadata.create_all(bind)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _schema_exists(inspector):
        return
    Base.metadata.drop_all(bind)
