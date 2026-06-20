"""Persistent command log stores for SQLite and PostgreSQL."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable
from uuid import UUID, uuid4

import db
from persistence.models import CommandLog as CommandLogModel
from persistence.session import init_schema, session_scope
from repositories import ContactStore
from services.command_log import CommandLogEntry, CommandStatus, InMemoryCommandLog


@runtime_checkable
class CommandLogStore(Protocol):
    def create(self, command_text: str, *, correlation_id: Optional[str] = None) -> CommandLogEntry: ...

    def get(self, command_id: UUID) -> Optional[CommandLogEntry]: ...

    def update(self, entry: CommandLogEntry) -> None: ...


def get_command_log_store(store: ContactStore) -> CommandLogStore:
    backend = getattr(store, "backend", None)
    if backend == "postgresql":
        return PostgresCommandLogStore(store.database_url)
    if backend == "sqlite":
        return SqliteCommandLogStore(store.database_path)
    return InMemoryCommandLog()


def _json_dumps(value: Optional[dict[str, Any]]) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _json_loads(value: Optional[str]) -> Optional[dict[str, Any]]:
    if not value:
        return None
    loaded = json.loads(value)
    return loaded if isinstance(loaded, dict) else None


def _dt_to_str(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat()


def _dt_from_str(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _sqlite_row_to_entry(row: Any) -> CommandLogEntry:
    return CommandLogEntry(
        id=UUID(row["id"]),
        command_text=row["command_text"],
        status=CommandStatus(row["status"]),
        intent=row["intent"],
        tool_name=row["tool_name"],
        tool_arguments=_json_loads(row["tool_arguments_json"]),
        risk_class=row["risk_class"],
        requires_approval=bool(row["requires_approval"]),
        approved_at=_dt_from_str(row["approved_at"]),
        result_summary=_json_loads(row["result_summary_json"]),
        error_code=row["error_code"],
        error_message=row["error_message"],
        correlation_id=row["correlation_id"],
        created_at=_dt_from_str(row["created_at"]) or datetime.now(),
        updated_at=_dt_from_str(row["updated_at"]) or datetime.now(),
    )


class SqliteCommandLogStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        db.init_db(database_path)

    def create(self, command_text: str, *, correlation_id: Optional[str] = None) -> CommandLogEntry:
        entry = CommandLogEntry(
            id=uuid4(),
            command_text=command_text,
            correlation_id=correlation_id,
        )
        with db.get_conn(self.database_path) as conn:
            conn.execute(
                """
                INSERT INTO command_log (
                    id, command_text, intent, tool_name, tool_arguments_json, risk_class,
                    status, requires_approval, approved_at, result_summary_json,
                    error_code, error_message, correlation_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(entry.id),
                    entry.command_text,
                    entry.intent,
                    entry.tool_name,
                    _json_dumps(entry.tool_arguments),
                    entry.risk_class,
                    entry.status.value,
                    int(entry.requires_approval),
                    _dt_to_str(entry.approved_at),
                    _json_dumps(entry.result_summary),
                    entry.error_code,
                    entry.error_message,
                    entry.correlation_id,
                    _dt_to_str(entry.created_at),
                    _dt_to_str(entry.updated_at),
                ),
            )
        return entry

    def get(self, command_id: UUID) -> Optional[CommandLogEntry]:
        with db.get_conn(self.database_path) as conn:
            row = conn.execute(
                "SELECT * FROM command_log WHERE id = ?",
                (str(command_id),),
            ).fetchone()
        return _sqlite_row_to_entry(row) if row else None

    def update(self, entry: CommandLogEntry) -> None:
        entry.updated_at = datetime.now(timezone.utc)
        with db.get_conn(self.database_path) as conn:
            conn.execute(
                """
                UPDATE command_log SET
                    command_text = ?,
                    intent = ?,
                    tool_name = ?,
                    tool_arguments_json = ?,
                    risk_class = ?,
                    status = ?,
                    requires_approval = ?,
                    approved_at = ?,
                    result_summary_json = ?,
                    error_code = ?,
                    error_message = ?,
                    correlation_id = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    entry.command_text,
                    entry.intent,
                    entry.tool_name,
                    _json_dumps(entry.tool_arguments),
                    entry.risk_class,
                    entry.status.value,
                    int(entry.requires_approval),
                    _dt_to_str(entry.approved_at),
                    _json_dumps(entry.result_summary),
                    entry.error_code,
                    entry.error_message,
                    entry.correlation_id,
                    _dt_to_str(entry.updated_at),
                    str(entry.id),
                ),
            )


def _model_to_entry(row: CommandLogModel) -> CommandLogEntry:
    return CommandLogEntry(
        id=row.id,
        command_text=row.command_text,
        status=CommandStatus(row.status),
        intent=row.intent,
        tool_name=row.tool_name,
        tool_arguments=dict(row.tool_arguments) if row.tool_arguments else None,
        risk_class=row.risk_class,
        requires_approval=bool(row.requires_approval),
        approved_at=row.approved_at,
        result_summary=dict(row.result_summary) if row.result_summary else None,
        error_code=row.error_code,
        error_message=row.error_message,
        correlation_id=row.correlation_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _apply_entry_to_model(entry: CommandLogEntry, row: CommandLogModel) -> None:
    row.command_text = entry.command_text
    row.intent = entry.intent
    row.tool_name = entry.tool_name
    row.tool_arguments = entry.tool_arguments
    row.risk_class = entry.risk_class
    row.status = entry.status.value
    row.requires_approval = entry.requires_approval
    row.approved_at = entry.approved_at
    row.result_summary = entry.result_summary
    row.error_code = entry.error_code
    row.error_message = entry.error_message
    row.correlation_id = entry.correlation_id
    row.updated_at = entry.updated_at


class PostgresCommandLogStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        init_schema(database_url)

    def create(self, command_text: str, *, correlation_id: Optional[str] = None) -> CommandLogEntry:
        entry = CommandLogEntry(
            id=uuid4(),
            command_text=command_text,
            correlation_id=correlation_id,
        )
        with session_scope(self.database_url) as session:
            row = CommandLogModel(
                id=entry.id,
                command_text=entry.command_text,
                status=entry.status.value,
                correlation_id=entry.correlation_id,
            )
            session.add(row)
            session.flush()
            entry.created_at = row.created_at
            entry.updated_at = row.updated_at
        return entry

    def get(self, command_id: UUID) -> Optional[CommandLogEntry]:
        with session_scope(self.database_url) as session:
            row = session.get(CommandLogModel, command_id)
            if row is None:
                return None
            return _model_to_entry(row)

    def update(self, entry: CommandLogEntry) -> None:
        entry.updated_at = datetime.now(timezone.utc)
        with session_scope(self.database_url) as session:
            row = session.get(CommandLogModel, entry.id)
            if row is None:
                row = CommandLogModel(id=entry.id, command_text=entry.command_text)
                session.add(row)
            _apply_entry_to_model(entry, row)
