"""SQLAlchemy models matching docs/data-model.md."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from persistence.base import Base, TimestampMixin, new_uuid


class Organization(Base, TimestampMixin):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    legacy_lead_id: Mapped[Optional[int]] = mapped_column(
        Integer, unique=True, index=True
    )
    name: Mapped[Optional[str]] = mapped_column(String(512))
    normalized_name: Mapped[Optional[str]] = mapped_column(String(512), index=True)
    website: Mapped[Optional[str]] = mapped_column(String(1024))
    normalized_domain: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    description: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    relevance_score: Mapped[int] = mapped_column(Integer, default=0)
    relevance_reason: Mapped[Optional[str]] = mapped_column(Text)
    legacy_metadata: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    archived_at: Mapped[Optional[datetime]] = mapped_column()

    people: Mapped[list["Person"]] = relationship(back_populates="organization")
    contact_methods: Mapped[list["ContactMethod"]] = relationship(
        back_populates="organization",
        foreign_keys="ContactMethod.organization_id",
    )
    interactions: Mapped[list["Interaction"]] = relationship(back_populates="organization")
    tasks: Mapped[list["Task"]] = relationship(back_populates="organization")


class Person(Base, TimestampMixin):
    __tablename__ = "people"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="SET NULL"), index=True
    )
    name: Mapped[Optional[str]] = mapped_column(String(512))
    normalized_name: Mapped[Optional[str]] = mapped_column(String(512), index=True)
    title: Mapped[Optional[str]] = mapped_column(String(255))
    is_decision_maker: Mapped[bool] = mapped_column(Boolean, default=False)
    relevance_score: Mapped[int] = mapped_column(Integer, default=0)
    relevance_reason: Mapped[Optional[str]] = mapped_column(Text)
    legacy_metadata: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)

    organization: Mapped[Optional[Organization]] = relationship(back_populates="people")
    contact_methods: Mapped[list["ContactMethod"]] = relationship(
        back_populates="person",
        foreign_keys="ContactMethod.person_id",
    )
    interactions: Mapped[list["Interaction"]] = relationship(back_populates="person")
    tasks: Mapped[list["Task"]] = relationship(back_populates="person")


class Source(Base, TimestampMixin):
    __tablename__ = "sources"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="SET NULL"), index=True
    )
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[Optional[str]] = mapped_column(String(2048))
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    captured_at: Mapped[Optional[datetime]] = mapped_column()
    created_by: Mapped[Optional[str]] = mapped_column(String(128))
    legacy_source_id: Mapped[Optional[int]] = mapped_column(Integer, unique=True, index=True)
    legacy_metadata: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)

    extractions: Mapped[list["Extraction"]] = relationship(back_populates="source")


class Extraction(Base, TimestampMixin):
    __tablename__ = "extractions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    model: Mapped[Optional[str]] = mapped_column(String(128))
    prompt_version: Mapped[Optional[str]] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="proposed", index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    structured_output: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    error_code: Mapped[Optional[str]] = mapped_column(String(64))
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    approved_at: Mapped[Optional[datetime]] = mapped_column()
    rejected_at: Mapped[Optional[datetime]] = mapped_column()

    source: Mapped[Source] = relationship(back_populates="extractions")


class ContactMethod(Base, TimestampMixin):
    __tablename__ = "contact_methods"
    __table_args__ = (
        CheckConstraint(
            "(organization_id IS NOT NULL AND person_id IS NULL) OR "
            "(organization_id IS NULL AND person_id IS NOT NULL)",
            name="ck_contact_method_single_owner",
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_contact_method_confidence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    person_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("people.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[str] = mapped_column(String(1024), nullable=False)
    normalized_value: Mapped[Optional[str]] = mapped_column(String(1024), index=True)
    source_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="SET NULL")
    )
    source_url: Mapped[Optional[str]] = mapped_column(String(2048))
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    verification_status: Mapped[str] = mapped_column(String(32), default="unverified")
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    discovered_at: Mapped[Optional[datetime]] = mapped_column()
    verified_at: Mapped[Optional[datetime]] = mapped_column()

    organization: Mapped[Optional[Organization]] = relationship(
        back_populates="contact_methods",
        foreign_keys=[organization_id],
    )
    person: Mapped[Optional[Person]] = relationship(
        back_populates="contact_methods",
        foreign_keys=[person_id],
    )


class Interaction(Base, TimestampMixin):
    __tablename__ = "interactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    person_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("people.id", ondelete="SET NULL")
    )
    kind: Mapped[Optional[str]] = mapped_column(String(64))
    occurred_at: Mapped[Optional[datetime]] = mapped_column()
    summary: Mapped[Optional[str]] = mapped_column(Text)
    source_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="SET NULL")
    )
    requires_followup: Mapped[bool] = mapped_column(Boolean, default=False)
    legacy_interaction_id: Mapped[Optional[int]] = mapped_column(Integer, unique=True, index=True)
    legacy_metadata: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)

    organization: Mapped[Optional[Organization]] = relationship(back_populates="interactions")
    person: Mapped[Optional[Person]] = relationship(back_populates="interactions")


class Task(Base, TimestampMixin):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    organization_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    person_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("people.id", ondelete="SET NULL")
    )
    title: Mapped[Optional[str]] = mapped_column(String(512))
    description: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="open")
    priority: Mapped[Optional[str]] = mapped_column(String(32))
    due_at: Mapped[Optional[datetime]] = mapped_column()
    completed_at: Mapped[Optional[datetime]] = mapped_column()
    created_by_command_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    legacy_task_id: Mapped[Optional[int]] = mapped_column(Integer, unique=True, index=True)
    legacy_metadata: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)

    organization: Mapped[Optional[Organization]] = relationship(back_populates="tasks")
    person: Mapped[Optional[Person]] = relationship(back_populates="tasks")


class Tag(Base, TimestampMixin):
    __tablename__ = "tags"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    normalized_name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)


class OrganizationTag(Base):
    __tablename__ = "organization_tags"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )


class PersonTag(Base):
    __tablename__ = "person_tags"

    person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("people.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )


class CommandLog(Base, TimestampMixin):
    __tablename__ = "command_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    command_text: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[Optional[str]] = mapped_column(String(128))
    tool_name: Mapped[Optional[str]] = mapped_column(String(128))
    tool_arguments: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    risk_class: Mapped[Optional[str]] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="received", index=True)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_at: Mapped[Optional[datetime]] = mapped_column()
    result_summary: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    error_code: Mapped[Optional[str]] = mapped_column(String(64))
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    correlation_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)


Index("idx_contact_methods_org_kind", ContactMethod.organization_id, ContactMethod.kind)
Index("idx_contact_methods_person_kind", ContactMethod.person_id, ContactMethod.kind)
