"""SQLite persistence for imported Gmail messages (G0)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import db
from gmail_schemas import (
    AttentionMarker,
    EmailDirection,
    LinkStatus,
    PrimaryIntent,
    SyncResultCounts,
    TemporalSignal,
    derive_requires_followup,
)

GMAIL_SCHEMA = """
CREATE TABLE IF NOT EXISTS gmail_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL DEFAULT 'gmail',
    external_account TEXT NOT NULL,
    external_message_id TEXT NOT NULL,
    external_thread_id TEXT,
    external_rfc_message_id TEXT,
    provider_occurred_at TEXT NOT NULL,
    provider_metadata_json TEXT,
    content_hash TEXT,
    raw_text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(provider, external_account, external_message_id)
);

CREATE INDEX IF NOT EXISTS idx_gmail_sources_thread
    ON gmail_sources(external_account, external_thread_id);

CREATE TABLE IF NOT EXISTS gmail_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gmail_source_id INTEGER NOT NULL REFERENCES gmail_sources(id) ON DELETE CASCADE,
    subject TEXT,
    direction TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    from_address TEXT,
    to_addresses_json TEXT,
    cc_addresses_json TEXT,
    primary_intent TEXT NOT NULL,
    intent_confidence REAL NOT NULL DEFAULT 0.0,
    markers_json TEXT NOT NULL,
    temporal_signals_json TEXT,
    link_status TEXT NOT NULL,
    classification_source TEXT NOT NULL,
    classification_model TEXT,
    classification_warning TEXT,
    requires_followup INTEGER NOT NULL DEFAULT 0,
    lead_id INTEGER REFERENCES leads(id) ON DELETE SET NULL,
    person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gmail_messages_occurred_at ON gmail_messages(occurred_at);
CREATE INDEX IF NOT EXISTS idx_gmail_messages_intent ON gmail_messages(primary_intent);
CREATE INDEX IF NOT EXISTS idx_gmail_messages_link_status ON gmail_messages(link_status);
CREATE INDEX IF NOT EXISTS idx_gmail_messages_lead_id ON gmail_messages(lead_id);

CREATE TABLE IF NOT EXISTS gmail_sync_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    account_email TEXT,
    configured_label TEXT NOT NULL,
    last_sync_at TEXT,
    last_success_at TEXT,
    last_status TEXT,
    last_result_summary_json TEXT,
    last_error_code TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_gmail_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(GMAIL_SCHEMA)


def _json_dumps(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _json_loads(value: Optional[str], default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def get_sync_state(conn: sqlite3.Connection, *, configured_label: str) -> Dict[str, Any]:
    row = conn.execute("SELECT * FROM gmail_sync_state WHERE id = 1").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO gmail_sync_state (id, configured_label) VALUES (1, ?)",
            (configured_label,),
        )
        row = conn.execute("SELECT * FROM gmail_sync_state WHERE id = 1").fetchone()
    return dict(row) if row else {}


def update_sync_state(
    conn: sqlite3.Connection,
    *,
    account_email: Optional[str],
    configured_label: str,
    last_sync_at: Optional[str],
    last_success_at: Optional[str],
    last_status: str,
    last_result_summary: Optional[dict[str, Any]],
    last_error_code: Optional[str],
) -> None:
    get_sync_state(conn, configured_label=configured_label)
    conn.execute(
        """
        UPDATE gmail_sync_state SET
            account_email = ?,
            configured_label = ?,
            last_sync_at = ?,
            last_success_at = ?,
            last_status = ?,
            last_result_summary_json = ?,
            last_error_code = ?
        WHERE id = 1
        """,
        (
            account_email,
            configured_label,
            last_sync_at,
            last_success_at,
            last_status,
            _json_dumps(last_result_summary),
            last_error_code,
        ),
    )


def find_existing_source(
    conn: sqlite3.Connection,
    *,
    external_account: str,
    external_message_id: str,
) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        """
        SELECT * FROM gmail_sources
        WHERE provider = 'gmail' AND external_account = ? AND external_message_id = ?
        """,
        (external_account.lower(), external_message_id),
    ).fetchone()
    return dict(row) if row else None


def get_message_by_source_id(conn: sqlite3.Connection, gmail_source_id: int) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM gmail_messages WHERE gmail_source_id = ?",
        (gmail_source_id,),
    ).fetchone()
    return dict(row) if row else None


def insert_gmail_source(
    conn: sqlite3.Connection,
    *,
    external_account: str,
    external_message_id: str,
    external_thread_id: str,
    external_rfc_message_id: Optional[str],
    provider_occurred_at: str,
    provider_metadata: dict[str, Any],
    content_hash: str,
    raw_text: str,
) -> Dict[str, Any]:
    conn.execute(
        """
        INSERT INTO gmail_sources (
            provider, external_account, external_message_id, external_thread_id,
            external_rfc_message_id, provider_occurred_at, provider_metadata_json,
            content_hash, raw_text, created_at
        ) VALUES ('gmail', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            external_account.lower(),
            external_message_id,
            external_thread_id,
            external_rfc_message_id,
            provider_occurred_at,
            _json_dumps(provider_metadata),
            content_hash,
            raw_text,
            _now(),
        ),
    )
    row = find_existing_source(
        conn,
        external_account=external_account,
        external_message_id=external_message_id,
    )
    if row is None:
        raise RuntimeError("gmail source insert failed")
    return row


def insert_gmail_message(
    conn: sqlite3.Connection,
    *,
    gmail_source_id: int,
    subject: Optional[str],
    direction: EmailDirection,
    occurred_at: str,
    from_address: str,
    to_addresses: list[dict[str, Any]],
    cc_addresses: list[dict[str, Any]],
    primary_intent: PrimaryIntent,
    intent_confidence: float,
    markers: list[AttentionMarker],
    temporal_signals: list[TemporalSignal],
    link_status: LinkStatus,
    classification_source: str,
    classification_model: Optional[str],
    classification_warning: Optional[str],
    lead_id: Optional[int],
    person_id: Optional[int],
) -> Dict[str, Any]:
    requires_followup = derive_requires_followup(markers)
    now = _now()
    conn.execute(
        """
        INSERT INTO gmail_messages (
            gmail_source_id, subject, direction, occurred_at, from_address,
            to_addresses_json, cc_addresses_json, primary_intent, intent_confidence,
            markers_json, temporal_signals_json, link_status, classification_source,
            classification_model, classification_warning, requires_followup,
            lead_id, person_id, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            gmail_source_id,
            subject,
            direction.value,
            occurred_at,
            from_address,
            _json_dumps(to_addresses),
            _json_dumps(cc_addresses),
            primary_intent.value,
            intent_confidence,
            _json_dumps([m.value for m in markers]),
            _json_dumps([t.model_dump(mode="json") for t in temporal_signals]),
            link_status.value,
            classification_source,
            classification_model,
            classification_warning,
            int(requires_followup),
            lead_id,
            person_id,
            now,
            now,
        ),
    )
    row = conn.execute(
        "SELECT * FROM gmail_messages WHERE gmail_source_id = ?",
        (gmail_source_id,),
    ).fetchone()
    return dict(row) if row else {}


def update_gmail_message(
    conn: sqlite3.Connection,
    message_id: int,
    *,
    content_hash: str,
    subject: Optional[str],
    direction: EmailDirection,
    occurred_at: str,
    from_address: str,
    to_addresses: list[dict[str, Any]],
    cc_addresses: list[dict[str, Any]],
    primary_intent: PrimaryIntent,
    intent_confidence: float,
    markers: list[AttentionMarker],
    temporal_signals: list[TemporalSignal],
    link_status: LinkStatus,
    classification_source: str,
    classification_model: Optional[str],
    classification_warning: Optional[str],
    lead_id: Optional[int],
    person_id: Optional[int],
    gmail_source_id: int,
) -> None:
    requires_followup = derive_requires_followup(markers)
    conn.execute(
        "UPDATE gmail_sources SET content_hash = ? WHERE id = ?",
        (content_hash, gmail_source_id),
    )
    conn.execute(
        """
        UPDATE gmail_messages SET
            subject = ?, direction = ?, occurred_at = ?, from_address = ?,
            to_addresses_json = ?, cc_addresses_json = ?, primary_intent = ?,
            intent_confidence = ?, markers_json = ?, temporal_signals_json = ?,
            link_status = ?, classification_source = ?, classification_model = ?,
            classification_warning = ?, requires_followup = ?, lead_id = ?,
            person_id = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            subject,
            direction.value,
            occurred_at,
            from_address,
            _json_dumps(to_addresses),
            _json_dumps(cc_addresses),
            primary_intent.value,
            intent_confidence,
            _json_dumps([m.value for m in markers]),
            _json_dumps([t.model_dump(mode="json") for t in temporal_signals]),
            link_status.value,
            classification_source,
            classification_model,
            classification_warning,
            int(requires_followup),
            lead_id,
            person_id,
            _now(),
            message_id,
        ),
    )


def find_thread_links(
    conn: sqlite3.Connection,
    *,
    external_account: str,
    external_thread_id: str,
    exclude_message_id: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT gm.lead_id, gm.person_id, gm.link_status
        FROM gmail_messages gm
        JOIN gmail_sources gs ON gs.id = gm.gmail_source_id
        WHERE gs.external_account = ?
          AND gs.external_thread_id = ?
          AND gs.external_message_id != ?
          AND gm.link_status = 'linked'
          AND gm.lead_id IS NOT NULL
        """,
        (external_account.lower(), external_thread_id, exclude_message_id),
    ).fetchall()
    return [dict(row) for row in rows]


def list_gmail_messages(
    conn: sqlite3.Connection,
    *,
    intent: Optional[str] = None,
    marker: Optional[str] = None,
    direction: Optional[str] = None,
    link_status: Optional[str] = None,
    lead_id: Optional[int] = None,
    person_id: Optional[int] = None,
    since: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    clauses = ["1=1"]
    params: list[Any] = []
    if intent:
        clauses.append("gm.primary_intent = ?")
        params.append(intent)
    if direction:
        clauses.append("gm.direction = ?")
        params.append(direction)
    if link_status:
        clauses.append("gm.link_status = ?")
        params.append(link_status)
    if lead_id is not None:
        clauses.append("gm.lead_id = ?")
        params.append(lead_id)
    if person_id is not None:
        clauses.append("gm.person_id = ?")
        params.append(person_id)
    if since:
        clauses.append("gm.occurred_at >= ?")
        params.append(since)
    if marker:
        clauses.append("gm.markers_json LIKE ?")
        params.append(f'%"{marker}"%')
    where = " AND ".join(clauses)
    count = conn.execute(
        f"""
        SELECT COUNT(*) FROM gmail_messages gm
        JOIN gmail_sources gs ON gs.id = gm.gmail_source_id
        WHERE {where}
        """,
        params,
    ).fetchone()[0]
    rows = conn.execute(
        f"""
        SELECT gm.*, gs.external_message_id, gs.external_thread_id, gs.external_account,
               gs.external_rfc_message_id, l.company_name, p.name AS person_name
        FROM gmail_messages gm
        JOIN gmail_sources gs ON gs.id = gm.gmail_source_id
        LEFT JOIN leads l ON l.id = gm.lead_id
        LEFT JOIN people p ON p.id = gm.person_id
        WHERE {where}
        ORDER BY gm.occurred_at DESC
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()
    return [dict(row) for row in rows], int(count)


def get_thread_messages(
    conn: sqlite3.Connection,
    *,
    external_thread_id: str,
    external_account: Optional[str] = None,
) -> list[dict[str, Any]]:
    clauses = ["gs.external_thread_id = ?"]
    params: list[Any] = [external_thread_id]
    if external_account:
        clauses.append("gs.external_account = ?")
        params.append(external_account.lower())
    where = " AND ".join(clauses)
    rows = conn.execute(
        f"""
        SELECT gm.*, gs.external_message_id, gs.external_thread_id, gs.external_account,
               gs.external_rfc_message_id, l.company_name, p.name AS person_name
        FROM gmail_messages gm
        JOIN gmail_sources gs ON gs.id = gm.gmail_source_id
        LEFT JOIN leads l ON l.id = gm.lead_id
        LEFT JOIN people p ON p.id = gm.person_id
        WHERE {where}
        ORDER BY gm.occurred_at ASC
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def format_local_time(iso_value: str, tz_name: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M %Z")
    except Exception:
        return iso_value


def row_to_public_dict(row: dict[str, Any], *, app_timezone: str) -> dict[str, Any]:
    markers = _json_loads(row.get("markers_json"), [])
    return {
        "id": row.get("id"),
        "gmail_source_id": row.get("gmail_source_id"),
        "external_message_id": row.get("external_message_id"),
        "external_thread_id": row.get("external_thread_id"),
        "thread_short": (row.get("external_thread_id") or "")[:12],
        "occurred_at": row.get("occurred_at"),
        "occurred_at_local": format_local_time(str(row.get("occurred_at") or ""), app_timezone),
        "direction": row.get("direction"),
        "from_address": row.get("from_address"),
        "to_addresses": _json_loads(row.get("to_addresses_json"), []),
        "subject": row.get("subject"),
        "lead_id": row.get("lead_id"),
        "person_id": row.get("person_id"),
        "company_name": row.get("company_name"),
        "person_name": row.get("person_name"),
        "primary_intent": row.get("primary_intent"),
        "intent_confidence": row.get("intent_confidence"),
        "markers": markers,
        "link_status": row.get("link_status"),
        "requires_followup": bool(row.get("requires_followup")),
        "classification_warning": row.get("classification_warning"),
    }


def init_gmail_db(db_path: Path) -> None:
    with db.get_conn(db_path) as conn:
        ensure_gmail_tables(conn)
