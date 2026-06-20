"""Initial PostgreSQL contact schema."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Schema is created via persistence.models Base.metadata.create_all / init_schema.
    # This revision exists as an Alembic baseline anchor for future migrations.
    pass


def downgrade() -> None:
    pass
