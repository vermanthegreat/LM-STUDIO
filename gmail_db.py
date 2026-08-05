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
    MessageRole,
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
    message_role TEXT NOT NULL DEFAULT 'conversation_message',
    target_company_name TEXT,
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
    link_reason TEXT NOT NULL DEFAULT 'unmatched',
    link_strength INTEGER NOT NULL DEFAULT 0,
    link_evidence_json TEXT,
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
    last_error_code TEXT,
    reauthorization_required INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS gmail_mailbox_sync_state (
    external_account TEXT NOT NULL,
    sync_mode TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'not_started',
    started_at TEXT, updated_at TEXT, completed_at TEXT,
    next_page_token TEXT, pages_processed INTEGER NOT NULL DEFAULT 0,
    messages_discovered INTEGER NOT NULL DEFAULT 0, messages_processed INTEGER NOT NULL DEFAULT 0,
    messages_imported INTEGER NOT NULL DEFAULT 0, messages_updated INTEGER NOT NULL DEFAULT 0,
    messages_already_present INTEGER NOT NULL DEFAULT 0, messages_failed INTEGER NOT NULL DEFAULT 0,
    last_error_code TEXT, latest_history_id TEXT,
    PRIMARY KEY (external_account, sync_mode)
);

CREATE TABLE IF NOT EXISTS gmail_conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_account TEXT NOT NULL, external_thread_id TEXT NOT NULL,
    lead_id INTEGER REFERENCES leads(id) ON DELETE SET NULL,
    primary_person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
    link_status TEXT NOT NULL, link_reason TEXT NOT NULL, link_confidence INTEGER NOT NULL DEFAULT 0,
    link_evidence_json TEXT,
    message_count INTEGER NOT NULL, first_message_at TEXT, last_message_at TEXT,
    first_outbound_at TEXT, last_outbound_at TEXT, first_inbound_at TEXT, last_inbound_at TEXT,
    last_message_direction TEXT, last_subject TEXT, requires_reply INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(external_account, external_thread_id)
);
CREATE INDEX IF NOT EXISTS idx_gmail_conversations_lead ON gmail_conversations(lead_id);
CREATE INDEX IF NOT EXISTS idx_gmail_conversations_person ON gmail_conversations(primary_person_id);
CREATE INDEX IF NOT EXISTS idx_gmail_conversations_status ON gmail_conversations(link_status);
CREATE INDEX IF NOT EXISTS idx_gmail_conversations_latest ON gmail_conversations(last_message_at);
CREATE INDEX IF NOT EXISTS idx_gmail_conversations_reply ON gmail_conversations(requires_reply);

CREATE TABLE IF NOT EXISTS lead_communication_state (
    lead_id INTEGER PRIMARY KEY REFERENCES leads(id) ON DELETE CASCADE,
    contacted INTEGER NOT NULL DEFAULT 0, first_contacted_at TEXT, last_contacted_at TEXT,
    replied INTEGER NOT NULL DEFAULT 0, first_reply_at TEXT, last_reply_at TEXT,
    last_message_at TEXT, last_message_direction TEXT, last_thread_id TEXT,
    primary_person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
    primary_external_email TEXT, conversation_count INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0, requires_reply INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_gmail_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(GMAIL_SCHEMA)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(gmail_messages)").fetchall()}
    if "message_role" not in columns:
        conn.execute(
            "ALTER TABLE gmail_messages ADD COLUMN message_role TEXT NOT NULL DEFAULT 'conversation_message'"
        )
    if "target_company_name" not in columns:
        conn.execute("ALTER TABLE gmail_messages ADD COLUMN target_company_name TEXT")
    if "link_reason" not in columns:
        conn.execute("ALTER TABLE gmail_messages ADD COLUMN link_reason TEXT NOT NULL DEFAULT 'unmatched'")
    if "link_strength" not in columns:
        conn.execute("ALTER TABLE gmail_messages ADD COLUMN link_strength INTEGER NOT NULL DEFAULT 0")
    if "link_evidence_json" not in columns:
        conn.execute("ALTER TABLE gmail_messages ADD COLUMN link_evidence_json TEXT")
    conv_columns = {row["name"] for row in conn.execute("PRAGMA table_info(gmail_conversations)").fetchall()}
    if "link_evidence_json" not in conv_columns:
        conn.execute("ALTER TABLE gmail_conversations ADD COLUMN link_evidence_json TEXT")
    sync_columns = {row["name"] for row in conn.execute("PRAGMA table_info(gmail_sync_state)").fetchall()}
    if "reauthorization_required" not in sync_columns:
        conn.execute(
            "ALTER TABLE gmail_sync_state ADD COLUMN reauthorization_required INTEGER NOT NULL DEFAULT 0"
        )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gmail_messages_role ON gmail_messages(message_role)")


def get_mailbox_sync_state(conn: sqlite3.Connection, *, external_account: str) -> Dict[str, Any]:
    row = conn.execute("SELECT * FROM gmail_mailbox_sync_state WHERE external_account = ? AND sync_mode = 'full_mailbox'", (external_account.lower(),)).fetchone()
    if row is None:
        now = _now()
        conn.execute("INSERT INTO gmail_mailbox_sync_state (external_account, sync_mode, status, updated_at) VALUES (?, 'full_mailbox', 'not_started', ?)", (external_account.lower(), now))
        row = conn.execute("SELECT * FROM gmail_mailbox_sync_state WHERE external_account = ? AND sync_mode = 'full_mailbox'", (external_account.lower(),)).fetchone()
    return dict(row) if row else {}


def update_mailbox_sync_state(conn: sqlite3.Connection, *, external_account: str, status: str, next_page_token: Optional[str], counts: SyncResultCounts, pages_processed: int, last_error_code: Optional[str] = None, completed: bool = False) -> None:
    previous = get_mailbox_sync_state(conn, external_account=external_account)
    now = _now()
    conn.execute("""UPDATE gmail_mailbox_sync_state SET status=?, started_at=COALESCE(started_at, ?), updated_at=?, completed_at=?, next_page_token=?, pages_processed=?, messages_discovered=?, messages_processed=?, messages_imported=?, messages_updated=?, messages_already_present=?, messages_failed=?, last_error_code=? WHERE external_account=? AND sync_mode='full_mailbox'""", (status, now, now, now if completed else previous.get("completed_at"), next_page_token, pages_processed, int(previous.get("messages_discovered") or 0) + counts.discovered, int(previous.get("messages_processed") or 0) + counts.discovered, int(previous.get("messages_imported") or 0) + counts.imported, int(previous.get("messages_updated") or 0) + counts.updated, int(previous.get("messages_already_present") or 0) + counts.already_present, int(previous.get("messages_failed") or 0) + counts.failed, last_error_code, external_account.lower()))


def rebuild_gmail_projections(conn: sqlite3.Connection) -> None:
    """Rebuild only derived CRM views from immutable Gmail source/message evidence."""
    now = _now()
    conn.execute("DELETE FROM gmail_conversations")
    rows = conn.execute("""SELECT gm.*, gs.external_account, gs.external_thread_id, gs.external_message_id
        FROM gmail_messages gm JOIN gmail_sources gs ON gs.id=gm.gmail_source_id
        ORDER BY gs.external_account, gs.external_thread_id, gm.occurred_at, gs.external_message_id""").fetchall()
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        item = dict(row); groups.setdefault((item["external_account"], item["external_thread_id"]), []).append(item)
    for (account, thread), messages in groups.items():
        lead_ids = {m["lead_id"] for m in messages if m["link_status"] == "linked" and m["lead_id"] is not None and int(m.get("link_strength") or 0) >= 200}
        if len(lead_ids) > 1:
            status, reason, strength, lead_id, person_id = "ambiguous", "conflicting_thread_links", 0, None, None
        elif lead_ids:
            lead_id = int(next(iter(lead_ids))); ranked = [m for m in messages if m["lead_id"] == lead_id]
            strongest = max(ranked, key=lambda m: (int(m.get("link_strength") or 0), -int(m["id"])))
            people = [m["person_id"] for m in ranked if m["person_id"] is not None and int(m.get("link_strength") or 0) >= 500]
            status, reason, strength, person_id = "linked", strongest.get("link_reason") or "thread_inherited", int(strongest.get("link_strength") or 0), min(people) if people else None
        else:
            status, reason, strength, lead_id, person_id = "unlinked", "unmatched", 0, None, None
        latest = messages[-1]
        evidence = _json_loads(str(strongest.get("link_evidence_json") if lead_ids else ""), {}) if lead_ids else {}
        if status == "ambiguous":
            evidence = {"candidate_lead_ids": sorted(lead_ids), "reason": reason}
        real_latest = latest["message_role"] == "conversation_message" and latest["primary_intent"] != "automated" and not latest.get("classification_warning")
        requires_reply = bool(latest["direction"] == "inbound" and real_latest)
        outbound = [m["occurred_at"] for m in messages if m["direction"] == "outbound"]
        inbound = [m["occurred_at"] for m in messages if m["direction"] == "inbound"]
        conn.execute("""INSERT INTO gmail_conversations (external_account,external_thread_id,lead_id,primary_person_id,link_status,link_reason,link_confidence,link_evidence_json,message_count,first_message_at,last_message_at,first_outbound_at,last_outbound_at,first_inbound_at,last_inbound_at,last_message_direction,last_subject,requires_reply,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (account,thread,lead_id,person_id,status,reason,strength,_json_dumps(evidence),len(messages),messages[0]["occurred_at"],latest["occurred_at"],outbound[0] if outbound else None,outbound[-1] if outbound else None,inbound[0] if inbound else None,inbound[-1] if inbound else None,latest["direction"],latest["subject"],int(requires_reply),now,now))
    conn.execute("DELETE FROM lead_communication_state")
    leads = conn.execute("SELECT DISTINCT lead_id FROM gmail_conversations WHERE lead_id IS NOT NULL AND link_status='linked'").fetchall()
    for lead_row in leads:
        lead_id = int(lead_row[0]); conversations = [dict(r) for r in conn.execute("SELECT * FROM gmail_conversations WHERE lead_id=? AND link_status='linked' ORDER BY last_message_at, id", (lead_id,)).fetchall()]
        messages = [dict(r) for r in conn.execute("SELECT gm.*, gs.external_thread_id FROM gmail_messages gm JOIN gmail_sources gs ON gs.id=gm.gmail_source_id WHERE gm.lead_id=? ORDER BY gm.occurred_at, gs.external_message_id", (lead_id,)).fetchall()]
        outbound = [m for m in messages if m["direction"] == "outbound"]
        first = outbound[0]["occurred_at"] if outbound else None
        inbound = [m for m in messages if first and m["direction"] == "inbound" and m["occurred_at"] >= first and m["message_role"] == "conversation_message" and m["primary_intent"] != "automated"]
        latest = conversations[-1] if conversations else {}
        conn.execute("INSERT INTO lead_communication_state (lead_id,contacted,first_contacted_at,last_contacted_at,replied,first_reply_at,last_reply_at,last_message_at,last_message_direction,last_thread_id,primary_person_id,primary_external_email,conversation_count,message_count,requires_reply,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (lead_id,int(bool(outbound)),first,outbound[-1]["occurred_at"] if outbound else None,int(bool(inbound)),inbound[0]["occurred_at"] if inbound else None,inbound[-1]["occurred_at"] if inbound else None,latest.get("last_message_at"),latest.get("last_message_direction"),latest.get("external_thread_id"),latest.get("primary_person_id"),None,len(conversations),len(messages),int(any(c["requires_reply"] for c in conversations)),now))


def list_conversations(conn: sqlite3.Connection, *, bucket: str, lead_id: Optional[int] = None, limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    clauses: list[str] = []; params: list[Any] = []
    if bucket == "agencies": clauses.append("c.link_status='linked' AND c.lead_id IS NOT NULL")
    elif bucket == "ambiguous": clauses.append("c.link_status='ambiguous'")
    elif bucket == "unmatched": clauses.append("c.link_status='unlinked' AND c.lead_id IS NULL")
    elif bucket != "all": raise ValueError("invalid conversation bucket")
    if lead_id is not None: clauses.append("c.lead_id=?"); params.append(lead_id)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    count = conn.execute("SELECT COUNT(*) FROM gmail_conversations c" + where, params).fetchone()[0]
    rows = conn.execute("SELECT c.*, l.company_name, p.name AS person_name FROM gmail_conversations c LEFT JOIN leads l ON l.id=c.lead_id LEFT JOIN people p ON p.id=c.primary_person_id" + where + " ORDER BY c.last_message_at DESC, c.id ASC LIMIT ? OFFSET ?", [*params, max(1,min(limit,100)), max(0,offset)]).fetchall()
    result = []
    for row in rows:
        item = dict(row); evidence = _json_loads(item.get("link_evidence_json"), {})
        ids = sorted({int(value) for value in evidence.get("candidate_lead_ids", []) if str(value).isdigit()})
        people = sorted({int(value) for value in evidence.get("candidate_person_ids", []) if str(value).isdigit()})
        item["candidate_agencies"] = [dict(r) for r in conn.execute("SELECT id, company_name FROM leads WHERE id IN (%s) ORDER BY id" % ",".join("?" * len(ids)), ids).fetchall()] if ids else []
        item["candidate_people"] = [dict(r) for r in conn.execute("SELECT id, name, lead_id, email FROM people WHERE id IN (%s) ORDER BY id" % ",".join("?" * len(people)), people).fetchall()] if people else []
        item["ambiguity_reason"] = item.get("link_reason")
        result.append(item)
    return result, int(count)


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
    reauthorization_required: bool = False,
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
            last_error_code = ?,
            reauthorization_required = ?
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
            int(reauthorization_required),
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
    message_role: MessageRole,
    target_company_name: Optional[str],
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
            to_addresses_json, cc_addresses_json, message_role, target_company_name,
            primary_intent, intent_confidence, markers_json, temporal_signals_json, link_status, classification_source,
            classification_model, classification_warning, requires_followup,
            lead_id, person_id, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            gmail_source_id,
            subject,
            direction.value,
            occurred_at,
            from_address,
            _json_dumps(to_addresses),
            _json_dumps(cc_addresses),
            message_role.value,
            target_company_name,
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
    message_role: MessageRole,
    target_company_name: Optional[str],
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
            to_addresses_json = ?, cc_addresses_json = ?, message_role = ?,
            target_company_name = ?, primary_intent = ?, intent_confidence = ?, markers_json = ?, temporal_signals_json = ?,
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
            message_role.value,
            target_company_name,
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
        SELECT gm.lead_id, gm.person_id, gm.link_status, gm.link_reason, gm.link_strength
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
               gs.external_rfc_message_id, gs.provider_occurred_at,
               gs.provider_metadata_json, gs.raw_text,
               l.company_name, p.name AS person_name
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
               gs.external_rfc_message_id, gs.provider_occurred_at,
               gs.provider_metadata_json, gs.raw_text,
               l.company_name, p.name AS person_name
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
    to_addresses = _json_loads(row.get("to_addresses_json"), [])
    cc_addresses = _json_loads(row.get("cc_addresses_json"), [])
    raw_text = str(row.get("raw_text") or "")
    return {
        "id": row.get("id"),
        "gmail_source_id": row.get("gmail_source_id"),
        "external_account": row.get("external_account"),
        "external_message_id": row.get("external_message_id"),
        "external_thread_id": row.get("external_thread_id"),
        "external_rfc_message_id": row.get("external_rfc_message_id"),
        "thread_short": (row.get("external_thread_id") or "")[:12],
        "message_short": (row.get("external_message_id") or "")[:12],
        "occurred_at": row.get("occurred_at"),
        "occurred_at_local": format_local_time(str(row.get("occurred_at") or ""), app_timezone),
        "provider_occurred_at": row.get("provider_occurred_at"),
        "direction": row.get("direction"),
        "from_address": row.get("from_address"),
        "to_addresses": to_addresses,
        "cc_addresses": cc_addresses,
        "to_address_text": ", ".join(
            str(item.get("email") or item.get("display_name") or "") for item in to_addresses if isinstance(item, dict)
        ),
        "cc_address_text": ", ".join(
            str(item.get("email") or item.get("display_name") or "") for item in cc_addresses if isinstance(item, dict)
        ),
        "subject": row.get("subject"),
        "message_role": row.get("message_role") or MessageRole.CONVERSATION_MESSAGE.value,
        "target_company_name": row.get("target_company_name"),
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
        "classification_source": row.get("classification_source"),
        "classification_model": row.get("classification_model"),
        "body_preview": raw_text[:500],
        "body_length": len(raw_text),
        "provider_metadata": _json_loads(row.get("provider_metadata_json"), {}),
    }


def init_gmail_db(db_path: Path) -> None:
    with db.get_conn(db_path) as conn:
        ensure_gmail_tables(conn)
