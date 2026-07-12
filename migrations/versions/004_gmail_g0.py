"""Gmail G0 schema for PostgreSQL."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "004_gmail_g0"
down_revision: Union[str, None] = "003_person_legacy_person_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(inspector: sa.Inspector, name: str) -> bool:
    return name in inspector.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "gmail_sources"):
        op.create_table(
            "gmail_sources",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("provider", sa.String(length=32), nullable=False),
            sa.Column("external_account", sa.String(length=320), nullable=False),
            sa.Column("external_message_id", sa.String(length=128), nullable=False),
            sa.Column("external_thread_id", sa.String(length=128), nullable=True),
            sa.Column("external_rfc_message_id", sa.String(length=512), nullable=True),
            sa.Column("provider_occurred_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("provider_metadata", sa.JSON(), nullable=True),
            sa.Column("content_hash", sa.String(length=64), nullable=True),
            sa.Column("raw_text", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "provider",
                "external_account",
                "external_message_id",
                name="uq_gmail_source_provider_account_message",
            ),
        )
        op.create_index("ix_gmail_sources_external_account", "gmail_sources", ["external_account"])
        op.create_index("ix_gmail_sources_external_thread_id", "gmail_sources", ["external_thread_id"])

    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "gmail_messages"):
        op.create_table(
            "gmail_messages",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("gmail_source_id", sa.Uuid(), nullable=False),
            sa.Column("organization_id", sa.Uuid(), nullable=True),
            sa.Column("person_id", sa.Uuid(), nullable=True),
            sa.Column("subject", sa.String(length=1024), nullable=True),
            sa.Column("direction", sa.String(length=32), nullable=False),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("from_address", sa.String(length=320), nullable=True),
            sa.Column("to_addresses", sa.JSON(), nullable=True),
            sa.Column("cc_addresses", sa.JSON(), nullable=True),
            sa.Column("primary_intent", sa.String(length=64), nullable=False),
            sa.Column("intent_confidence", sa.Float(), nullable=False),
            sa.Column("markers", sa.JSON(), nullable=True),
            sa.Column("temporal_signals", sa.JSON(), nullable=True),
            sa.Column("link_status", sa.String(length=32), nullable=False),
            sa.Column("classification_source", sa.String(length=32), nullable=False),
            sa.Column("classification_model", sa.String(length=128), nullable=True),
            sa.Column("classification_warning", sa.String(length=128), nullable=True),
            sa.Column("requires_followup", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["gmail_source_id"], ["gmail_sources.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["person_id"], ["people.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_gmail_messages_occurred_at", "gmail_messages", ["occurred_at"])
        op.create_index("ix_gmail_messages_primary_intent", "gmail_messages", ["primary_intent"])
        op.create_index("ix_gmail_messages_link_status", "gmail_messages", ["link_status"])

    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "gmail_sync_state"):
        op.create_table(
            "gmail_sync_state",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("account_email", sa.String(length=320), nullable=True),
            sa.Column("configured_label", sa.String(length=128), nullable=False),
            sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_status", sa.String(length=32), nullable=True),
            sa.Column("last_result_summary", sa.JSON(), nullable=True),
            sa.Column("last_error_code", sa.String(length=64), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _table_exists(inspector, "gmail_messages"):
        op.drop_table("gmail_messages")
    inspector = sa.inspect(bind)
    if _table_exists(inspector, "gmail_sources"):
        op.drop_table("gmail_sources")
    inspector = sa.inspect(bind)
    if _table_exists(inspector, "gmail_sync_state"):
        op.drop_table("gmail_sync_state")
