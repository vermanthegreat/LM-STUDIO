"""SQLite persistence for lead intelligence app."""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from discovery_models import DiscoveryRequest
from research_job_models import (
    ADAPTER_KEYS,
    DEFAULT_LEASE_SECONDS,
    MAX_LEASE_SECONDS,
    MAX_WORKER_ID_LENGTH,
    MIN_LEASE_SECONDS,
    RESEARCH_JOB_ACTIVE_STATES,
    ResearchJobCreate,
    ResearchJobError,
    ResearchJobListFilter,
    ResearchJobRecord,
    ResearchJobStatus,
    canonical_request_json,
    research_intent_key,
)

DB_PATH = Path(__file__).parent / "leads.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT,
    normalized_name TEXT,
    website TEXT,
    domain TEXT,
    company_email TEXT,
    company_phone TEXT,
    partner_tier TEXT,
    plus_partner_signal INTEGER DEFAULT 0,
    rating REAL,
    review_count INTEGER,
    partner_since TEXT,
    primary_location TEXT,
    supported_locations_json TEXT,
    languages_json TEXT,
    featured_work_json TEXT,
    services_json TEXT,
    locations_json TEXT,
    industries_json TEXT,
    description TEXT,
    fit_score INTEGER DEFAULT 0,
    status TEXT DEFAULT 'new',
    confidence REAL DEFAULT 0.0,
    extraction_status TEXT DEFAULT 'ok',
    enrichment_status TEXT DEFAULT 'pending',
    possible_duplicate INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leads_normalized_name ON leads(normalized_name);
CREATE INDEX IF NOT EXISTS idx_leads_domain ON leads(domain);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);

CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER REFERENCES leads(id) ON DELETE CASCADE,
    name TEXT,
    title TEXT,
    department TEXT,
    seniority TEXT,
    email TEXT,
    linkedin_url TEXT,
    is_decision_maker INTEGER DEFAULT 0,
    is_relevant_contact INTEGER DEFAULT 0,
    role_type TEXT,
    relevance_reason TEXT,
    confidence REAL DEFAULT 0.0,
    email_status TEXT DEFAULT 'unknown',
    email_confidence REAL DEFAULT 0.0,
    last_verified_at TEXT,
    raw_source_id INTEGER REFERENCES raw_sources(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_people_lead_id ON people(lead_id);
CREATE INDEX IF NOT EXISTS idx_people_linkedin ON people(linkedin_url);

CREATE TABLE IF NOT EXISTS raw_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER REFERENCES leads(id) ON DELETE SET NULL,
    source_type TEXT NOT NULL,
    source_url TEXT,
    source_filter_tier TEXT,
    raw_text TEXT NOT NULL,
    parsed_json TEXT,
    extraction_status TEXT DEFAULT 'ok',
    confidence REAL DEFAULT 0.0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_raw_sources_lead_id ON raw_sources(lead_id);

CREATE TABLE IF NOT EXISTS interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER REFERENCES leads(id) ON DELETE CASCADE,
    person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
    type TEXT,
    subject TEXT,
    body TEXT,
    summary TEXT,
    reply_needed INTEGER DEFAULT 0,
    deadline TEXT,
    priority TEXT,
    next_action TEXT,
    status TEXT DEFAULT 'open',
    raw_source_id INTEGER REFERENCES raw_sources(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_interactions_lead_id ON interactions(lead_id);
CREATE INDEX IF NOT EXISTS idx_interactions_deadline ON interactions(deadline);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER REFERENCES leads(id) ON DELETE CASCADE,
    person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
    title TEXT,
    due_date TEXT,
    priority TEXT,
    status TEXT DEFAULT 'open',
    source_interaction_id INTEGER REFERENCES interactions(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_lead_id ON tasks(lead_id);
CREATE INDEX IF NOT EXISTS idx_tasks_due_date ON tasks(due_date);

CREATE TABLE IF NOT EXISTS command_log (
    id TEXT PRIMARY KEY,
    command_text TEXT NOT NULL,
    intent TEXT,
    tool_name TEXT,
    tool_arguments_json TEXT,
    risk_class TEXT,
    status TEXT NOT NULL DEFAULT 'received',
    requires_approval INTEGER DEFAULT 0,
    approved_at TEXT,
    result_summary_json TEXT,
    error_code TEXT,
    error_message TEXT,
    correlation_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_command_log_status ON command_log(status);
CREATE INDEX IF NOT EXISTS idx_command_log_correlation_id ON command_log(correlation_id);

CREATE TABLE IF NOT EXISTS person_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    raw_source_id INTEGER NOT NULL REFERENCES raw_sources(id) ON DELETE CASCADE,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    normalized_name TEXT NOT NULL CHECK (length(trim(normalized_name)) > 0),
    title TEXT,
    role_type TEXT NOT NULL,
    is_decision_maker INTEGER NOT NULL CHECK (is_decision_maker IN (0, 1)),
    profile_url TEXT,
    source_type TEXT NOT NULL,
    source_url TEXT,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    relevance_reason TEXT,
    discovery_method TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'needs_review',
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    applied_person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((status = 'applied' AND applied_person_id IS NOT NULL) OR
           (status <> 'applied' AND applied_person_id IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_person_candidates_lead ON person_candidates(lead_id);
CREATE INDEX IF NOT EXISTS idx_person_candidates_reuse_profile ON person_candidates(lead_id, raw_source_id, profile_url);
CREATE INDEX IF NOT EXISTS idx_person_candidates_reuse_name ON person_candidates(lead_id, raw_source_id, normalized_name, role_type);
CREATE INDEX IF NOT EXISTS idx_person_candidates_status ON person_candidates(status);

CREATE TABLE IF NOT EXISTS contact_method_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    raw_source_id INTEGER NOT NULL REFERENCES raw_sources(id) ON DELETE CASCADE,
    person_candidate_id INTEGER REFERENCES person_candidates(id) ON DELETE CASCADE,
    person_id INTEGER REFERENCES people(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    value TEXT NOT NULL CHECK (length(trim(value)) > 0),
    normalized_value TEXT NOT NULL CHECK (length(trim(normalized_value)) > 0),
    source_url TEXT,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    verification_status TEXT NOT NULL DEFAULT 'unverified',
    evidence_basis TEXT NOT NULL,
    discovery_method TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'needs_review',
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    applied_contact_method_id INTEGER,
    discovered_at TEXT NOT NULL,
    verified_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((person_candidate_id IS NOT NULL AND person_id IS NULL) OR
           (person_candidate_id IS NULL AND person_id IS NOT NULL)),
    CHECK ((status = 'applied' AND applied_contact_method_id IS NOT NULL) OR
           (status <> 'applied' AND applied_contact_method_id IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_contact_candidates_lead ON contact_method_candidates(lead_id);
CREATE INDEX IF NOT EXISTS idx_contact_candidates_reuse_candidate ON contact_method_candidates(person_candidate_id, raw_source_id, kind, normalized_value);
CREATE INDEX IF NOT EXISTS idx_contact_candidates_reuse_person ON contact_method_candidates(person_id, raw_source_id, kind, normalized_value);
CREATE INDEX IF NOT EXISTS idx_contact_candidates_status ON contact_method_candidates(status);

CREATE TABLE IF NOT EXISTS research_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    adapter_key TEXT NOT NULL,
    intent_key TEXT NOT NULL,
    request_job_id TEXT NOT NULL,
    request_snapshot_json TEXT NOT NULL,
    target_roles_json TEXT NOT NULL,
    approved_source_types_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'claimed', 'running', 'retry_wait', 'succeeded', 'partial', 'no_result', 'needs_review', 'failed', 'cancelled', 'abandoned')),
    priority INTEGER NOT NULL CHECK (priority >= 0 AND priority <= 100),
    requested_result_limit INTEGER NOT NULL CHECK (requested_result_limit >= 1 AND requested_result_limit <= 5),
    max_pages INTEGER NOT NULL CHECK (max_pages >= 1 AND max_pages <= 5),
    max_requests INTEGER NOT NULL CHECK (max_requests >= 1 AND max_requests <= 5),
    timeout_seconds INTEGER NOT NULL CHECK (timeout_seconds >= 1 AND timeout_seconds <= 30),
    max_attempts INTEGER NOT NULL CHECK (max_attempts >= 1 AND max_attempts <= 3),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    not_before TEXT,
    requested_by TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    provider_config_ref TEXT NOT NULL,
    claimed_by TEXT,
    lease_token TEXT,
    claimed_at TEXT,
    lease_expires_at TEXT,
    started_at TEXT,
    completed_at TEXT,
    result_summary_json TEXT,
    safe_error_code TEXT,
    retry_after TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_research_jobs_active_intent
    ON research_jobs(intent_key)
    WHERE status IN ('queued', 'claimed', 'running', 'retry_wait');
CREATE INDEX IF NOT EXISTS idx_research_jobs_lead_created
    ON research_jobs(lead_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_research_jobs_queue
    ON research_jobs(status, not_before, priority, created_at, id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_name(name: Optional[str]) -> str:
    if not name:
        return ""
    cleaned = re.sub(r"[^\w\s&.-]", " ", name.lower())
    return " ".join(cleaned.split())


def sanitize_company_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return name
    return name.lstrip("\ufeff\u200b\u200c\u200d").strip()


def merge_company_name(existing: Optional[str], new: Optional[str]) -> Optional[str]:
    """Keep stored name when an update would drop the first character."""
    new = sanitize_company_name(new)
    if not new:
        return None
    existing = sanitize_company_name(existing) or ""
    if not existing:
        return new
    if len(new) == len(existing) - 1 and existing[1:] == new:
        return existing
    if len(new) == len(existing) - 1 and existing[1:].lower() == new.lower():
        return existing
    return new


def extract_domain(website: Optional[str]) -> Optional[str]:
    if not website:
        return None
    url = website.strip()
    if not url:
        return None
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        host = urlparse(url).netloc or urlparse(url).path
        host = host.lower().removeprefix("www.")
        return host.split("/")[0] if host else None
    except Exception:
        return None


PERSONAL_EMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "live.com", "icloud.com", "protonmail.com", "aol.com",
})


def normalize_email(email: Optional[str]) -> Optional[str]:
    if not email:
        return None
    text = str(email).strip()
    m = re.search(r"<([^>]+@[^>]+)>", text)
    if m:
        text = m.group(1)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text).strip().lower()
    return text if "@" in text else None


def email_domain(email: Optional[str]) -> Optional[str]:
    norm = normalize_email(email)
    if not norm:
        return None
    return norm.split("@")[-1]


def is_business_email(email: Optional[str]) -> bool:
    domain = email_domain(email)
    return bool(domain and domain not in PERSONAL_EMAIL_DOMAINS)


def normalize_linkedin_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    u = url.strip().rstrip("/").lower()
    u = re.sub(r"\?.*$", "", u)
    return u or None


@contextmanager
def get_conn(db_path: Path = DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path = DB_PATH) -> None:
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(raw_sources)").fetchall()}
    if "source_filter_tier" not in cols:
        conn.execute("ALTER TABLE raw_sources ADD COLUMN source_filter_tier TEXT")

    lead_cols = {row[1] for row in conn.execute("PRAGMA table_info(leads)").fetchall()}
    lead_migrations = {
        "company_email": "TEXT",
        "company_phone": "TEXT",
        "plus_partner_signal": "INTEGER DEFAULT 0",
        "rating": "REAL",
        "review_count": "INTEGER",
        "partner_since": "TEXT",
        "primary_location": "TEXT",
        "supported_locations_json": "TEXT",
        "languages_json": "TEXT",
        "featured_work_json": "TEXT",
        "enrichment_status": "TEXT DEFAULT 'pending'",
    }
    for col, col_type in lead_migrations.items():
        if col not in lead_cols:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {col_type}")

    people_cols = {row[1] for row in conn.execute("PRAGMA table_info(people)").fetchall()}
    people_migrations = {
        "email": "TEXT",
        "role_type": "TEXT",
        "email_status": "TEXT DEFAULT 'unknown'",
        "email_confidence": "REAL DEFAULT 0.0",
        "last_verified_at": "TEXT",
        "updated_at": "TEXT",
    }
    for col, col_type in people_migrations.items():
        if col not in people_cols:
            conn.execute(f"ALTER TABLE people ADD COLUMN {col} {col_type}")
    from models import EMAIL_STATUSES, ROLE_TYPES, normalize_email_enrichment
    from scoring import derive_person_role_fields
    for row in conn.execute("SELECT * FROM people").fetchall():
        stored_role = row["role_type"]
        role_type = (
            stored_role
            if stored_role in ROLE_TYPES
            else "other"
            if stored_role
            else None
        )
        stored_email_status = (
            row["email_status"] if row["email_status"] in EMAIL_STATUSES else "unknown"
        )
        stored_email_confidence = row["email_confidence"]
        try:
            numeric_email_confidence = float(stored_email_confidence or 0.0)
        except (TypeError, ValueError):
            numeric_email_confidence = 0.0
        if not 0.0 <= numeric_email_confidence <= 1.0:
            numeric_email_confidence = 0.0
        classification = derive_person_role_fields(row["title"], role_type)
        email_metadata = normalize_email_enrichment(
            has_email=bool(normalize_email(row["email"])),
            email_status=stored_email_status,
            email_confidence=numeric_email_confidence,
            last_verified_at=row["last_verified_at"],
        )
        conn.execute(
            """UPDATE people SET role_type = ?, is_decision_maker = ?,
               is_relevant_contact = ?, relevance_reason = ?,
               email_status = ?, email_confidence = ?, last_verified_at = ?,
               updated_at = COALESCE(updated_at, created_at)
               WHERE id = ?""",
            (
                classification["role_type"],
                int(classification["is_decision_maker"]),
                int(classification["is_relevant_contact"]),
                classification["relevance_reason"],
                email_metadata["email_status"],
                email_metadata["email_confidence"],
                email_metadata["last_verified_at"],
                row["id"],
            ),
        )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_people_email ON people(email)")

    task_cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    if "created_by_command_id" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN created_by_command_id TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_created_by_command_id "
        "ON tasks(created_by_command_id) WHERE created_by_command_id IS NOT NULL"
    )

    from gmail_db import ensure_gmail_tables

    ensure_gmail_tables(conn)


def _json_dumps(obj: Any) -> Optional[str]:
    if obj is None:
        return None
    return json.dumps(obj, ensure_ascii=False)


def _json_loads(text: Optional[str], default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


def find_matching_leads(
    company_name: Optional[str] = None,
    website: Optional[str] = None,
    linkedin_url: Optional[str] = None,
    db_path: Path = DB_PATH,
    conn: Optional[sqlite3.Connection] = None,
) -> List[Dict[str, Any]]:
    def _run(c: sqlite3.Connection) -> List[Dict[str, Any]]:
        norm = normalize_name(company_name)
        domain = extract_domain(website)
        li = normalize_linkedin_url(linkedin_url)
        clauses: List[str] = []
        params: List[Any] = []
        if norm:
            clauses.append("normalized_name = ?")
            params.append(norm)
        if domain:
            clauses.append("domain = ?")
            params.append(domain)
        if not clauses:
            return []
        sql = f"SELECT * FROM leads WHERE {' OR '.join(clauses)}"
        rows = c.execute(sql, params).fetchall()
        results = [dict(r) for r in rows]
        if li:
            person_rows = c.execute(
                "SELECT DISTINCT lead_id FROM people WHERE linkedin_url = ?", (li,)
            ).fetchall()
            lead_ids = {r["lead_id"] for r in person_rows}
            for row in results:
                lead_ids.discard(row["id"])
            if lead_ids:
                extra = c.execute(
                    f"SELECT * FROM leads WHERE id IN ({','.join('?' * len(lead_ids))})",
                    list(lead_ids),
                ).fetchall()
                results.extend(dict(r) for r in extra)
        return results

    if conn is not None:
        return _run(conn)
    with get_conn(db_path) as c:
        return _run(c)


def find_company_identity_candidates(
    evidence_kind: str,
    value: str,
    db_path: Path = DB_PATH,
    conn: Optional[sqlite3.Connection] = None,
) -> List[Dict[str, Any]]:
    """Return leads matching one exact, normalized company-identity fact."""
    allowed = {
        "domain", "email_domain", "linkedin_company_url", "normalized_name", "source_alias",
    }
    if evidence_kind not in allowed or not value:
        return []

    def _run(c: sqlite3.Connection) -> List[Dict[str, Any]]:
        lead_ids: set[int] = set()
        if evidence_kind == "domain":
            rows = c.execute("SELECT id FROM leads WHERE lower(domain) = ?", (value,)).fetchall()
            lead_ids.update(int(row["id"]) for row in rows)
        elif evidence_kind == "email_domain":
            rows = c.execute(
                """SELECT id FROM leads
                   WHERE lower(domain) = ?
                      OR lower(substr(company_email, instr(company_email, '@') + 1)) = ?""",
                (value, value),
            ).fetchall()
            lead_ids.update(int(row["id"]) for row in rows)
        elif evidence_kind == "normalized_name":
            rows = c.execute("SELECT id FROM leads WHERE normalized_name = ?", (value,)).fetchall()
            lead_ids.update(int(row["id"]) for row in rows)
        else:
            rows = c.execute(
                "SELECT lead_id, parsed_json FROM raw_sources WHERE lead_id IS NOT NULL"
            ).fetchall()
            for row in rows:
                parsed = _json_loads(row["parsed_json"], {}) or {}
                if evidence_kind == "linkedin_company_url":
                    stored = normalize_linkedin_url(parsed.get("linkedin_company_url"))
                    if stored == value:
                        lead_ids.add(int(row["lead_id"]))
                else:
                    aliases = parsed.get("company_aliases") or []
                    if any(normalize_name(alias) == value for alias in aliases if isinstance(alias, str)):
                        lead_ids.add(int(row["lead_id"]))
        if not lead_ids:
            return []
        placeholders = ",".join("?" for _ in lead_ids)
        rows = c.execute(
            f"SELECT * FROM leads WHERE id IN ({placeholders}) ORDER BY id", sorted(lead_ids)
        ).fetchall()
        return [_hydrate_lead_row(dict(row)) for row in rows]

    if conn is not None:
        return _run(conn)
    with get_conn(db_path) as c:
        return _run(c)


def find_leads_by_email(
    email: str,
    db_path: Path = DB_PATH,
    conn: Optional[sqlite3.Connection] = None,
) -> List[Dict[str, Any]]:
    norm = normalize_email(email)
    if not norm:
        return []
    domain = email_domain(norm)
    if not domain or domain in PERSONAL_EMAIL_DOMAINS:
        return []

    def _run(c: sqlite3.Connection) -> List[Dict[str, Any]]:
        rows = c.execute(
            """SELECT * FROM leads
               WHERE lower(domain) = ? OR lower(company_email) = ?""",
            (domain, norm),
        ).fetchall()
        return [_hydrate_lead_row(dict(r)) for r in rows]

    if conn is not None:
        return _run(conn)
    with get_conn(db_path) as c:
        return _run(c)


def find_exact_email_matches(
    email: str,
    db_path: Path = DB_PATH,
    conn: Optional[sqlite3.Connection] = None,
) -> List[Dict[str, Any]]:
    norm = normalize_email(email)
    if not norm:
        return []

    def _run(c: sqlite3.Connection) -> List[Dict[str, Any]]:
        matches: List[Dict[str, Any]] = []
        people_rows = c.execute(
            "SELECT id, lead_id, email FROM people WHERE lower(email) = ?",
            (norm,),
        ).fetchall()
        for row in people_rows:
            matches.append(
                {
                    "lead_id": row["lead_id"],
                    "person_id": row["id"],
                    "kind": "person",
                }
            )
        lead_rows = c.execute(
            "SELECT id FROM leads WHERE lower(company_email) = ?",
            (norm,),
        ).fetchall()
        for row in lead_rows:
            matches.append(
                {
                    "lead_id": row["id"],
                    "person_id": None,
                    "kind": "organization",
                }
            )
        return matches

    if conn is not None:
        return _run(conn)
    with get_conn(db_path) as c:
        return _run(c)


def create_raw_source(
    source_type: str,
    raw_text: str,
    source_url: Optional[str] = None,
    source_filter_tier: Optional[str] = None,
    parsed_json: Optional[Dict[str, Any]] = None,
    extraction_status: str = "ok",
    confidence: float = 0.0,
    lead_id: Optional[int] = None,
    db_path: Path = DB_PATH,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    now = _now()

    def _run(c: sqlite3.Connection) -> Dict[str, Any]:
        cur = c.execute(
            """INSERT INTO raw_sources
               (lead_id, source_type, source_url, source_filter_tier, raw_text, parsed_json,
                extraction_status, confidence, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                lead_id,
                source_type,
                source_url,
                source_filter_tier,
                raw_text,
                _json_dumps(parsed_json),
                extraction_status,
                confidence,
                now,
            ),
        )
        row = c.execute("SELECT * FROM raw_sources WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)

    if conn is not None:
        return _run(conn)
    with get_conn(db_path) as c:
        return _run(c)


def _candidate_connection(db_path: Path, conn: Optional[sqlite3.Connection]):
    if conn is not None:
        @contextmanager
        def _existing_connection():
            yield conn
        return _existing_connection()
    return get_conn(db_path)


def _begin_candidate_write(c: sqlite3.Connection) -> None:
    """Serialize the candidate read-then-insert section at SQLite's write boundary."""
    if not c.in_transaction:
        c.execute("BEGIN IMMEDIATE")


def _candidate_refs(c: sqlite3.Connection, lead_id: int, raw_source_id: int) -> sqlite3.Row:
    lead = c.execute("SELECT id FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if lead is None:
        from candidate_models import CandidateError
        raise CandidateError("lead_not_found", "lead not found")
    source = c.execute("SELECT lead_id FROM raw_sources WHERE id = ?", (raw_source_id,)).fetchone()
    if source is None:
        from candidate_models import CandidateError
        raise CandidateError("raw_source_not_found", "raw source not found")
    if source["lead_id"] != lead_id:
        from candidate_models import CandidateError
        raise CandidateError("raw_source_ownership_mismatch", "raw source belongs to another lead")
    return source


def _candidate_status(status: str, applied_id: Optional[int]) -> None:
    from candidate_models import CANDIDATE_STATUSES, CandidateError, require_enum

    require_enum(status, CANDIDATE_STATUSES, "invalid_candidate_status", "candidate status")
    if status == "applied" and applied_id is None:
        raise CandidateError("invalid_applied_binding", "applied status requires an applied identifier")
    if status != "applied" and applied_id is not None:
        raise CandidateError("invalid_applied_binding", "only applied status may have an applied identifier")


def create_or_reuse_person_candidate(
    lead_id: int,
    raw_source_id: int,
    *,
    name: str,
    title: Optional[str] = None,
    role_type: str = "other",
    is_decision_maker: bool = False,
    profile_url: Optional[str] = None,
    source_type: str,
    source_url: Optional[str] = None,
    confidence: float = 0.0,
    relevance_reason: Optional[str] = None,
    discovery_method: str,
    db_path: Path = DB_PATH,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    from candidate_models import DISCOVERY_METHODS, CandidateError, normalize_profile_url, require_confidence, require_enum, normalize_candidate_name
    normalized_name = normalize_candidate_name(name)
    profile_url = normalize_profile_url(profile_url)
    require_enum(role_type, __import__("models").ROLE_TYPES, "invalid_role_type", "role_type")
    require_enum(discovery_method, DISCOVERY_METHODS, "invalid_discovery_method", "discovery_method")
    numeric_confidence = require_confidence(confidence)

    def _run(c: sqlite3.Connection) -> Dict[str, Any]:
        _begin_candidate_write(c)
        _candidate_refs(c, lead_id, raw_source_id)
        query = """SELECT * FROM person_candidates
                   WHERE lead_id = ? AND raw_source_id = ? AND
                   ((? IS NOT NULL AND profile_url = ?) OR
                    (? IS NULL AND normalized_name = ? AND role_type = ?))
                   ORDER BY id LIMIT 1"""
        existing = c.execute(query, (lead_id, raw_source_id, profile_url, profile_url, profile_url, normalized_name, role_type)).fetchone()
        if existing is not None:
            return dict(existing)
        now = _now()
        cur = c.execute(
            """INSERT INTO person_candidates
               (lead_id, raw_source_id, name, normalized_name, title, role_type,
                is_decision_maker, profile_url, source_type, source_url, confidence,
                relevance_reason, discovery_method, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (lead_id, raw_source_id, name.strip(), normalized_name, title, role_type,
             int(is_decision_maker), profile_url, source_type, source_url, numeric_confidence,
             relevance_reason, discovery_method, now, now),
        )
        return dict(c.execute("SELECT * FROM person_candidates WHERE id = ?", (cur.lastrowid,)).fetchone())

    with _candidate_connection(db_path, conn) as c:
        return _run(c)


def get_person_candidate(candidate_id: int, db_path: Path = DB_PATH, conn: Optional[sqlite3.Connection] = None) -> Optional[Dict[str, Any]]:
    def _run(c: sqlite3.Connection):
        row = c.execute("SELECT * FROM person_candidates WHERE id = ?", (candidate_id,)).fetchone()
        return dict(row) if row else None
    with _candidate_connection(db_path, conn) as c:
        return _run(c)


def list_person_candidates_for_lead(lead_id: int, db_path: Path = DB_PATH, conn: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    def _run(c: sqlite3.Connection):
        return [dict(row) for row in c.execute("SELECT * FROM person_candidates WHERE lead_id = ? ORDER BY id", (lead_id,)).fetchall()]
    with _candidate_connection(db_path, conn) as c:
        return _run(c)


def update_person_candidate_status(candidate_id: int, expected_version: int, target_status: str, applied_person_id: Optional[int] = None, db_path: Path = DB_PATH, conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    from candidate_models import CandidateError
    _candidate_status(target_status, applied_person_id)
    def _run(c: sqlite3.Connection):
        row = c.execute("SELECT * FROM person_candidates WHERE id = ?", (candidate_id,)).fetchone()
        if row is None:
            raise CandidateError("candidate_not_found", "person candidate not found")
        if row["version"] != expected_version:
            raise CandidateError("stale_candidate_version", "candidate version is stale")
        if applied_person_id is not None:
            person = c.execute("SELECT lead_id FROM people WHERE id = ?", (applied_person_id,)).fetchone()
            if person is None or person["lead_id"] != row["lead_id"]:
                raise CandidateError("person_ownership_mismatch", "applied person belongs to another lead")
        now = _now()
        result = c.execute("UPDATE person_candidates SET status = ?, applied_person_id = ?, version = version + 1, updated_at = ? WHERE id = ? AND version = ?", (target_status, applied_person_id, now, candidate_id, expected_version))
        if result.rowcount != 1:
            raise CandidateError("stale_candidate_version", "candidate version is stale")
        return dict(c.execute("SELECT * FROM person_candidates WHERE id = ?", (candidate_id,)).fetchone())
    with _candidate_connection(db_path, conn) as c:
        return _run(c)


def create_or_reuse_contact_candidate(
    lead_id: int,
    raw_source_id: int,
    *,
    person_candidate_id: Optional[int] = None,
    person_id: Optional[int] = None,
    kind: str,
    value: str,
    normalized_value: Optional[str] = None,
    source_url: Optional[str] = None,
    confidence: float = 0.0,
    verification_status: str = "unverified",
    evidence_basis: str = "source_confirmed",
    discovery_method: str,
    discovered_at: Optional[str] = None,
    verified_at: Optional[str] = None,
    db_path: Path = DB_PATH,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    from candidate_models import CONTACT_KINDS, DISCOVERY_METHODS, CandidateError, normalize_contact_value, require_confidence, require_enum, validate_verification
    if (person_candidate_id is None) == (person_id is None):
        raise CandidateError("invalid_owner_binding", "exactly one candidate owner is required")
    require_enum(kind, CONTACT_KINDS, "invalid_contact_kind", "contact kind")
    require_enum(discovery_method, DISCOVERY_METHODS, "invalid_discovery_method", "discovery_method")
    normalized = normalized_value.strip() if normalized_value else normalize_contact_value(kind, value)
    if not normalized:
        raise CandidateError("invalid_contact_value", "normalized contact value cannot be blank")
    numeric_confidence = require_confidence(confidence)
    validate_verification(evidence_basis, verification_status, verified_at)

    def _run(c: sqlite3.Connection):
        _begin_candidate_write(c)
        _candidate_refs(c, lead_id, raw_source_id)
        if person_candidate_id is not None:
            owner = c.execute("SELECT lead_id FROM person_candidates WHERE id = ?", (person_candidate_id,)).fetchone()
            if owner is None or owner["lead_id"] != lead_id:
                raise CandidateError("person_candidate_ownership_mismatch", "person candidate belongs to another lead")
            owner_clause, owner_value = "person_candidate_id = ?", person_candidate_id
        else:
            owner = c.execute("SELECT lead_id FROM people WHERE id = ?", (person_id,)).fetchone()
            if owner is None or owner["lead_id"] != lead_id:
                raise CandidateError("person_ownership_mismatch", "person belongs to another lead")
            owner_clause, owner_value = "person_id = ?", person_id
        existing = c.execute(f"SELECT * FROM contact_method_candidates WHERE {owner_clause} AND raw_source_id = ? AND kind = ? AND normalized_value = ? LIMIT 1", (owner_value, raw_source_id, kind, normalized)).fetchone()
        if existing is not None:
            return dict(existing)
        now = _now()
        cur = c.execute("""INSERT INTO contact_method_candidates
            (lead_id, raw_source_id, person_candidate_id, person_id, kind, value,
             normalized_value, source_url, confidence, verification_status, evidence_basis,
             discovery_method, discovered_at, created_at, updated_at, verified_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (lead_id, raw_source_id, person_candidate_id, person_id, kind, value.strip(), normalized,
             source_url, numeric_confidence, verification_status, evidence_basis, discovery_method,
             discovered_at or now, now, now, verified_at))
        return dict(c.execute("SELECT * FROM contact_method_candidates WHERE id = ?", (cur.lastrowid,)).fetchone())
    with _candidate_connection(db_path, conn) as c:
        return _run(c)


def get_contact_candidate(candidate_id: int, db_path: Path = DB_PATH, conn: Optional[sqlite3.Connection] = None) -> Optional[Dict[str, Any]]:
    def _run(c):
        row = c.execute("SELECT * FROM contact_method_candidates WHERE id = ?", (candidate_id,)).fetchone()
        return dict(row) if row else None
    with _candidate_connection(db_path, conn) as c:
        return _run(c)


def list_contact_candidates_for_lead(lead_id: int, db_path: Path = DB_PATH, conn: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    def _run(c):
        return [dict(row) for row in c.execute("SELECT * FROM contact_method_candidates WHERE lead_id = ? ORDER BY id", (lead_id,)).fetchall()]
    with _candidate_connection(db_path, conn) as c:
        return _run(c)


def update_contact_candidate_status(candidate_id: int, expected_version: int, target_status: str, applied_contact_method_id: Optional[int] = None, db_path: Path = DB_PATH, conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    from candidate_models import CandidateError
    _candidate_status(target_status, applied_contact_method_id)
    def _run(c):
        row = c.execute("SELECT * FROM contact_method_candidates WHERE id = ?", (candidate_id,)).fetchone()
        if row is None:
            raise CandidateError("candidate_not_found", "contact candidate not found")
        if row["version"] != expected_version:
            raise CandidateError("stale_candidate_version", "candidate version is stale")
        now = _now()
        result = c.execute("UPDATE contact_method_candidates SET status = ?, applied_contact_method_id = ?, version = version + 1, updated_at = ? WHERE id = ? AND version = ?", (target_status, applied_contact_method_id, now, candidate_id, expected_version))
        if result.rowcount != 1:
            raise CandidateError("stale_candidate_version", "candidate version is stale")
        return dict(c.execute("SELECT * FROM contact_method_candidates WHERE id = ?", (candidate_id,)).fetchone())
    with _candidate_connection(db_path, conn) as c:
        return _run(c)


def upsert_lead(
    data: Dict[str, Any],
    lead_id: Optional[int] = None,
    db_path: Path = DB_PATH,
    conn: Optional[sqlite3.Connection] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Create or update a lead. Returns (lead, is_new)."""
    now = _now()
    company_name = sanitize_company_name(data.get("company_name"))
    normalized = normalize_name(company_name) or data.get("normalized_name", "")
    website = data.get("website")
    domain = extract_domain(website) or data.get("domain")

    matches = find_matching_leads(company_name, website, db_path=db_path, conn=conn)
    possible_duplicate = len(matches) > 1 or (
        len(matches) == 1 and lead_id and matches[0]["id"] != lead_id
    )

    target_id = lead_id
    is_new = False
    if not target_id and matches:
        target_id = matches[0]["id"]
    if not target_id:
        is_new = True

    fields = {
        "company_name": company_name,
        "normalized_name": normalized,
        "website": website,
        "domain": domain,
        "company_email": data.get("company_email"),
        "company_phone": data.get("company_phone"),
        "partner_tier": data.get("partner_tier"),
        "plus_partner_signal": int(bool(data.get("plus_partner_signal"))),
        "rating": data.get("rating"),
        "review_count": data.get("review_count"),
        "partner_since": data.get("partner_since"),
        "primary_location": data.get("primary_location"),
        "supported_locations_json": _json_dumps(data.get("supported_locations")),
        "languages_json": _json_dumps(data.get("languages")),
        "featured_work_json": _json_dumps(data.get("featured_work")),
        "services_json": _json_dumps(data.get("services")),
        "locations_json": _json_dumps(data.get("locations")),
        "industries_json": _json_dumps(data.get("industries")),
        "description": data.get("description"),
        "fit_score": data.get("fit_score", 0),
        "status": data.get("status", "new"),
        "confidence": data.get("confidence", 0.0),
        "extraction_status": data.get("extraction_status", "ok"),
        "enrichment_status": data.get("enrichment_status", "pending"),
        "possible_duplicate": 1 if possible_duplicate else 0,
        "updated_at": now,
    }

    def _run(c: sqlite3.Connection) -> Tuple[Dict[str, Any], bool]:
        nonlocal target_id, is_new
        if is_new:
            fields["created_at"] = now
            cols = ", ".join(fields.keys())
            placeholders = ", ".join("?" * len(fields))
            cur = c.execute(
                f"INSERT INTO leads ({cols}) VALUES ({placeholders})",
                list(fields.values()),
            )
            target_id = cur.lastrowid
        else:
            if "enrichment_status" not in data:
                fields.pop("enrichment_status", None)
            existing = c.execute("SELECT company_name FROM leads WHERE id = ?", (target_id,)).fetchone()
            merged_name = merge_company_name(
                existing["company_name"] if existing else None,
                company_name,
            )
            if merged_name is not None:
                fields["company_name"] = merged_name
                fields["normalized_name"] = normalize_name(merged_name)
            elif company_name is None:
                fields.pop("company_name", None)
                fields.pop("normalized_name", None)
            update_fields = {k: v for k, v in fields.items() if v is not None}
            sets = ", ".join(f"{k} = ?" for k in update_fields)
            c.execute(
                f"UPDATE leads SET {sets} WHERE id = ?",
                list(update_fields.values()) + [target_id],
            )
        row = c.execute("SELECT * FROM leads WHERE id = ?", (target_id,)).fetchone()
        return dict(row), is_new

    if conn is not None:
        return _run(conn)
    with get_conn(db_path) as c:
        return _run(c)


def add_person(
    lead_id: int,
    data: Dict[str, Any],
    raw_source_id: Optional[int] = None,
    db_path: Path = DB_PATH,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    now = _now()
    li = normalize_linkedin_url(data.get("linkedin_url"))
    em = normalize_email(data.get("email"))
    normalized_person_name = normalize_name(data.get("name"))
    title = (data.get("title") or "").strip() or None
    from models import EMAIL_STATUSES, normalize_email_enrichment
    from scoring import derive_person_role_fields

    role_fields = derive_person_role_fields(title, data.get("role_type"))
    email_metadata = normalize_email_enrichment(
        has_email=bool(em),
        email_status=data.get("email_status"),
        email_confidence=data.get("email_confidence"),
        last_verified_at=data.get("last_verified_at"),
    )

    def _stronger_email_status(existing_status: Optional[str]) -> str:
        priority = {
            "unknown": 0,
            "inferred": 1,
            "pattern_derived": 2,
            "published": 3,
            "verified": 4,
        }
        current = existing_status if existing_status in EMAIL_STATUSES else "unknown"
        incoming_status = email_metadata["email_status"]
        return incoming_status if priority[incoming_status] > priority[current] else current

    def _name_title_conflict(existing: sqlite3.Row) -> bool:
        existing_title = (existing["title"] or "").strip()
        if not existing_title or not title:
            return False
        old_role = derive_person_role_fields(existing_title, existing["role_type"])["role_type"]
        return old_role != role_fields["role_type"]

    def _run(c: sqlite3.Connection) -> Dict[str, Any]:
        existing = None
        if li:
            existing = c.execute(
                "SELECT * FROM people WHERE lead_id = ? AND linkedin_url = ?",
                (lead_id, li),
            ).fetchone()
        if not existing and em:
            existing = c.execute(
                "SELECT * FROM people WHERE lead_id = ? AND lower(email) = ?",
                (lead_id, em),
            ).fetchone()
        if not existing and not li and not em and normalized_person_name:
            candidates = [
                row
                for row in c.execute(
                    "SELECT * FROM people WHERE lead_id = ? AND name IS NOT NULL",
                    (lead_id,),
                ).fetchall()
                if normalize_name(row["name"]) == normalized_person_name
                and not _name_title_conflict(row)
            ]
            existing = candidates[0] if len(candidates) == 1 else None
        if existing:
            effective_role = role_fields
            title_update = title
            existing_role = derive_person_role_fields(
                existing["title"], existing["role_type"]
            )
            if (
                data.get("role_type") is None
                and role_fields["role_type"] == "other"
                and existing_role["role_type"] != "other"
            ):
                effective_role = existing_role
                title_update = None

            existing_email = normalize_email(existing["email"])
            same_email = bool(em and (not existing_email or existing_email == em))
            if same_email:
                merged_email_status = _stronger_email_status(existing["email_status"])
                merged_email_confidence = max(
                    float(existing["email_confidence"] or 0.0),
                    email_metadata["email_confidence"],
                )
                merged_last_verified_at = existing["last_verified_at"]
                if merged_email_status == "verified":
                    merged_last_verified_at = (
                        email_metadata["last_verified_at"] or merged_last_verified_at
                    )
                else:
                    merged_last_verified_at = None
            else:
                merged_email_status = (
                    existing["email_status"]
                    if existing["email_status"] in EMAIL_STATUSES
                    else "unknown"
                )
                merged_email_confidence = float(existing["email_confidence"] or 0.0)
                merged_last_verified_at = (
                    existing["last_verified_at"]
                    if merged_email_status == "verified"
                    else None
                )
            c.execute(
                """UPDATE people SET title=COALESCE(?,title),
                   department=COALESCE(?,department), seniority=COALESCE(?,seniority),
                   email=COALESCE(email,?), linkedin_url=COALESCE(linkedin_url,?),
                   is_decision_maker=?, is_relevant_contact=?, role_type=?,
                   relevance_reason=?,
                   confidence=?, email_status=?, email_confidence=?,
                   last_verified_at=?,
                   raw_source_id=COALESCE(?,raw_source_id), updated_at=?
                   WHERE id=?""",
                (
                    title_update,
                    data.get("department"),
                    data.get("seniority"),
                    em,
                    li,
                    int(effective_role["is_decision_maker"]),
                    int(effective_role["is_relevant_contact"]),
                    effective_role["role_type"],
                    effective_role["relevance_reason"],
                    data.get("confidence", 0.0),
                    merged_email_status,
                    merged_email_confidence,
                    merged_last_verified_at,
                    raw_source_id,
                    now,
                    existing["id"],
                ),
            )
            row = c.execute("SELECT * FROM people WHERE id = ?", (existing["id"],)).fetchone()
            return dict(row)
        cur = c.execute(
            """INSERT INTO people
               (lead_id, name, title, department, seniority, email, linkedin_url,
                is_decision_maker, is_relevant_contact, role_type, relevance_reason,
                confidence, email_status, email_confidence, last_verified_at,
                raw_source_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                lead_id,
                data.get("name"),
                title,
                data.get("department"),
                data.get("seniority"),
                em,
                li,
                int(role_fields["is_decision_maker"]),
                int(role_fields["is_relevant_contact"]),
                role_fields["role_type"],
                role_fields["relevance_reason"],
                data.get("confidence", 0.0),
                email_metadata["email_status"],
                email_metadata["email_confidence"],
                email_metadata["last_verified_at"],
                raw_source_id,
                now,
                now,
            ),
        )
        row = c.execute("SELECT * FROM people WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)

    if conn is not None:
        return _run(conn)
    with get_conn(db_path) as c:
        return _run(c)


def add_interaction(
    lead_id: int,
    data: Dict[str, Any],
    raw_source_id: Optional[int] = None,
    db_path: Path = DB_PATH,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    now = _now()

    def _run(c: sqlite3.Connection) -> Dict[str, Any]:
        cur = c.execute(
            """INSERT INTO interactions
               (lead_id, person_id, type, subject, body, summary, reply_needed,
                deadline, priority, next_action, status, raw_source_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                lead_id,
                data.get("person_id"),
                data.get("type", "email"),
                data.get("subject"),
                data.get("body"),
                data.get("summary"),
                int(data.get("reply_needed", 0)),
                data.get("deadline"),
                data.get("priority"),
                data.get("next_action"),
                data.get("status", "open"),
                raw_source_id,
                now,
            ),
        )
        row = c.execute("SELECT * FROM interactions WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)

    if conn is not None:
        return _run(conn)
    with get_conn(db_path) as c:
        return _run(c)


def add_task(
    lead_id: int,
    data: Dict[str, Any],
    db_path: Path = DB_PATH,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    now = _now()
    created_by_command_id = data.get("created_by_command_id")

    def _find_existing(c: sqlite3.Connection) -> Optional[Dict[str, Any]]:
        if not created_by_command_id:
            return None
        row = c.execute(
            "SELECT * FROM tasks WHERE created_by_command_id = ?",
            (str(created_by_command_id),),
        ).fetchone()
        return dict(row) if row else None

    def _run(c: sqlite3.Connection) -> Dict[str, Any]:
        existing = _find_existing(c)
        if existing is not None:
            return existing
        cur = c.execute(
            """INSERT INTO tasks
               (lead_id, person_id, title, due_date, priority, status,
                source_interaction_id, created_at, created_by_command_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                lead_id,
                data.get("person_id"),
                data.get("title"),
                data.get("due_date"),
                data.get("priority"),
                data.get("status", "open"),
                data.get("source_interaction_id"),
                now,
                str(created_by_command_id) if created_by_command_id else None,
            ),
        )
        row = c.execute("SELECT * FROM tasks WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)

    if conn is not None:
        return _run(conn)
    with get_conn(db_path) as c:
        return _run(c)


def find_task_by_created_by_command_id(
    command_id: str,
    db_path: Path = DB_PATH,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[Dict[str, Any]]:
    def _run(c: sqlite3.Connection) -> Optional[Dict[str, Any]]:
        row = c.execute(
            "SELECT * FROM tasks WHERE created_by_command_id = ?",
            (str(command_id),),
        ).fetchone()
        return dict(row) if row else None

    if conn is not None:
        return _run(conn)
    with get_conn(db_path) as c:
        return _run(c)


def link_raw_source_to_lead(
    raw_source_id: int,
    lead_id: int,
    db_path: Path = DB_PATH,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    def _run(c: sqlite3.Connection) -> None:
        c.execute("UPDATE raw_sources SET lead_id = ? WHERE id = ?", (lead_id, raw_source_id))

    if conn is not None:
        _run(conn)
    else:
        with get_conn(db_path) as c:
            _run(c)


def _hydrate_lead_row(d: Dict[str, Any]) -> Dict[str, Any]:
    d["services"] = _json_loads(d.pop("services_json", None), [])
    d["locations"] = _json_loads(d.pop("locations_json", None), [])
    d["industries"] = _json_loads(d.pop("industries_json", None), [])
    d["supported_locations"] = _json_loads(d.pop("supported_locations_json", None), [])
    d["languages"] = _json_loads(d.pop("languages_json", None), [])
    d["featured_work"] = _json_loads(d.pop("featured_work_json", None), [])
    d["plus_partner_signal"] = bool(d.get("plus_partner_signal"))
    return d


def list_leads(
    db_path: Path = DB_PATH,
    research_only: bool = False,
) -> List[Dict[str, Any]]:
    base_sql = """
    SELECT l.*,
           (SELECT COUNT(*) FROM people p WHERE p.lead_id = l.id) AS people_count,
           (SELECT COUNT(*) FROM people p WHERE p.lead_id = l.id AND p.is_decision_maker = 1) > 0 AS has_decision_maker,
           (SELECT MAX(i.created_at) FROM interactions i WHERE i.lead_id = l.id) AS last_interaction,
           (SELECT MIN(COALESCE(t.due_date, i.deadline))
            FROM leads l2
            LEFT JOIN tasks t ON t.lead_id = l2.id AND t.status = 'open'
            LEFT JOIN interactions i ON i.lead_id = l2.id AND i.status = 'open' AND i.deadline IS NOT NULL
            WHERE l2.id = l.id) AS next_deadline
    FROM leads l
    """
    if research_only:
        sql = f"""SELECT * FROM ({base_sql}) AS lead_stats
        WHERE enrichment_status IN ('pending', 'in_progress', 'needs_review')
          AND (people_count < 2 OR has_decision_maker = 0)
        ORDER BY fit_score DESC, has_decision_maker ASC, people_count ASC, updated_at DESC"""
    else:
        sql = base_sql + " ORDER BY l.fit_score DESC, l.updated_at DESC"
    with get_conn(db_path) as conn:
        rows = conn.execute(sql).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        _hydrate_lead_row(d)
        d["has_decision_maker"] = bool(d.get("has_decision_maker"))
        result.append(d)
    return result


def get_raw_sources_for_lead(
    lead_id: int,
    db_path: Path = DB_PATH,
    conn: Optional[sqlite3.Connection] = None,
) -> List[Dict[str, Any]]:
    """Return raw sources linked to a lead, newest first."""

    def _run(c: sqlite3.Connection) -> List[Dict[str, Any]]:
        rows = c.execute(
            "SELECT * FROM raw_sources WHERE lead_id = ? ORDER BY created_at DESC",
            (lead_id,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["parsed_json"] = _json_loads(d.get("parsed_json"))
            result.append(d)
        return result

    if conn is not None:
        return _run(conn)
    with get_conn(db_path) as c:
        return _run(c)


def get_lead(
    lead_id: int,
    db_path: Path = DB_PATH,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[Dict[str, Any]]:
    def _run(c: sqlite3.Connection) -> Optional[Dict[str, Any]]:
        row = c.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if not row:
            return None
        lead = _hydrate_lead_row(dict(row))
        lead["people"] = [dict(r) for r in c.execute(
            """SELECT p.*, rs.source_type, rs.source_url
               FROM people p LEFT JOIN raw_sources rs ON rs.id = p.raw_source_id
               WHERE p.lead_id = ? ORDER BY p.is_decision_maker DESC, p.name""",
            (lead_id,),
        ).fetchall()]
        lead["interactions"] = [dict(r) for r in c.execute(
            "SELECT * FROM interactions WHERE lead_id = ? ORDER BY created_at DESC",
            (lead_id,),
        ).fetchall()]
        lead["tasks"] = [dict(r) for r in c.execute(
            "SELECT * FROM tasks WHERE lead_id = ? ORDER BY due_date IS NULL, due_date ASC",
            (lead_id,),
        ).fetchall()]
        lead["raw_sources"] = get_raw_sources_for_lead(lead_id, conn=c)
        lead["canonical_source"] = lead["raw_sources"][0] if lead["raw_sources"] else None
        lead["source_history"] = lead["raw_sources"][1:] if len(lead["raw_sources"]) > 1 else []
        lead["source_filter_tier"] = next(
            (rs.get("source_filter_tier") for rs in lead["raw_sources"] if rs.get("source_filter_tier")),
            None,
        )
        counts = c.execute(
            """SELECT
                   (SELECT COUNT(*) FROM people p WHERE p.lead_id = ?) AS people_count,
                   EXISTS(SELECT 1 FROM people p WHERE p.lead_id = ? AND p.is_decision_maker = 1)
                       AS has_decision_maker""",
            (lead_id, lead_id),
        ).fetchone()
        lead["people_count"] = counts["people_count"] if counts else len(lead["people"])
        lead["has_decision_maker"] = bool(counts["has_decision_maker"]) if counts else False
        return lead

    if conn is not None:
        return _run(conn)
    with get_conn(db_path) as c:
        return _run(c)


def get_all_leads_simple(db_path: Path = DB_PATH) -> List[Dict[str, Any]]:
    with get_conn(db_path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, company_name FROM leads ORDER BY company_name"
        ).fetchall()]


def count_potential_clients(db_path: Path = DB_PATH) -> int:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM leads WHERE status NOT IN ('closed', 'archived')"
        ).fetchone()
        return row["c"] if row else 0


def get_top_leads(limit: int = 10, db_path: Path = DB_PATH) -> List[Dict[str, Any]]:
    return list_leads(db_path)[:limit]


def get_contact_summary(db_path: Path = DB_PATH) -> Dict[str, int]:
    active = "status NOT IN ('closed', 'archived')"
    active_lead = "l.status NOT IN ('closed', 'archived')"
    with get_conn(db_path) as conn:
        companies = conn.execute(f"SELECT COUNT(*) AS c FROM leads WHERE {active}").fetchone()["c"]
        with_company_email = conn.execute(
            f"""SELECT COUNT(*) AS c FROM leads
                WHERE {active} AND company_email IS NOT NULL AND trim(company_email) != ''"""
        ).fetchone()["c"]
        with_people = conn.execute(
            f"""SELECT COUNT(DISTINCT l.id) AS c FROM leads l
                JOIN people p ON p.lead_id = l.id WHERE {active_lead}"""
        ).fetchone()["c"]
        with_person_email = conn.execute(
            f"""SELECT COUNT(DISTINCT l.id) AS c FROM leads l
                JOIN people p ON p.lead_id = l.id
                WHERE {active_lead} AND p.email IS NOT NULL AND trim(p.email) != ''"""
        ).fetchone()["c"]
        with_any_email = conn.execute(
            f"""SELECT COUNT(*) AS c FROM leads l
                WHERE {active_lead} AND (
                    (l.company_email IS NOT NULL AND trim(l.company_email) != '')
                    OR EXISTS (
                        SELECT 1 FROM people p
                        WHERE p.lead_id = l.id AND p.email IS NOT NULL AND trim(p.email) != ''
                    )
                )"""
        ).fetchone()["c"]
        email_interactions = conn.execute(
            "SELECT COUNT(*) AS c FROM interactions WHERE lower(type) = 'email'"
        ).fetchone()["c"]
    return {
        "companies": companies,
        "with_company_email": with_company_email,
        "with_people": with_people,
        "with_person_email": with_person_email,
        "with_any_email": with_any_email,
        "with_verified_email": 0,
        "without_email": companies - with_any_email,
        "email_interactions": email_interactions,
    }


def list_contact_emails(limit: int = 50, db_path: Path = DB_PATH) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with get_conn(db_path) as conn:
        for r in conn.execute(
            """SELECT l.id AS lead_id, l.company_name, l.company_email AS email,
                      NULL AS person_name, 'company' AS source
               FROM leads l
               WHERE l.company_email IS NOT NULL AND trim(l.company_email) != ''
               ORDER BY l.company_name COLLATE NOCASE"""
        ):
            rows.append(dict(r))
        for r in conn.execute(
            """SELECT l.id AS lead_id, l.company_name, p.email,
                      p.name AS person_name, 'person' AS source
               FROM people p
               JOIN leads l ON l.id = p.lead_id
               WHERE p.email IS NOT NULL AND trim(p.email) != ''
               ORDER BY l.company_name COLLATE NOCASE, p.name COLLATE NOCASE"""
        ):
            rows.append(dict(r))
    return rows[:limit]


def get_leads_without_email(db_path: Path = DB_PATH) -> List[Dict[str, Any]]:
    sql = """
    SELECT l.* FROM leads l
    WHERE l.status NOT IN ('closed', 'archived')
      AND (l.company_email IS NULL OR trim(l.company_email) = '')
      AND NOT EXISTS (
          SELECT 1 FROM people p
          WHERE p.lead_id = l.id AND p.email IS NOT NULL AND trim(p.email) != ''
      )
    ORDER BY l.fit_score DESC
    """
    with get_conn(db_path) as conn:
        rows = conn.execute(sql).fetchall()
    return [_hydrate_lead_row(dict(r)) for r in rows]


def get_leads_without_contacts(db_path: Path = DB_PATH) -> List[Dict[str, Any]]:
    sql = """
    SELECT l.* FROM leads l
    WHERE NOT EXISTS (SELECT 1 FROM people p WHERE p.lead_id = l.id)
    ORDER BY l.fit_score DESC
    """
    with get_conn(db_path) as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def get_followups_due(
    db_path: Path = DB_PATH,
    due_on_or_before: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> List[Dict[str, Any]]:
    cutoff = str(due_on_or_before or _now()[:10])[:10]
    sql = """
    SELECT l.company_name, l.id AS lead_id, t.title, t.due_date, t.priority, 'task' AS item_type
    FROM tasks t JOIN leads l ON l.id = t.lead_id
    WHERE t.status = 'open' AND t.due_date IS NOT NULL AND t.due_date <= ?
    UNION ALL
    SELECT l.company_name, l.id, i.subject, i.deadline, i.priority, 'interaction'
    FROM interactions i JOIN leads l ON l.id = i.lead_id
    WHERE i.status = 'open' AND i.deadline IS NOT NULL AND i.deadline <= ?
    ORDER BY due_date ASC
    """
    if conn is not None:
        rows = conn.execute(sql, (cutoff, cutoff)).fetchall()
        return [dict(r) for r in rows]
    with get_conn(db_path) as conn:
        rows = conn.execute(sql, (cutoff, cutoff)).fetchall()
    return [dict(r) for r in rows]


def search_lead_by_name(name: str, db_path: Path = DB_PATH) -> List[Dict[str, Any]]:
    norm = normalize_name(name)
    like = f"%{norm}%"
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM leads WHERE normalized_name LIKE ? OR company_name LIKE ?",
            (like, f"%{name}%"),
        ).fetchall()
    return [dict(r) for r in rows]


def update_lead_fit_score(
    lead_id: int,
    fit_score: int,
    db_path: Path = DB_PATH,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    def _run(c: sqlite3.Connection) -> None:
        c.execute(
            "UPDATE leads SET fit_score = ?, updated_at = ? WHERE id = ?",
            (fit_score, _now(), lead_id),
        )

    if conn is not None:
        _run(conn)
    else:
        with get_conn(db_path) as c:
            _run(c)


ALLOWED_LEAD_CONTACT_UPDATE_FIELDS = frozenset({"company_email", "company_phone", "website"})


def update_lead_contact_field(
    lead_id: int,
    field: str,
    value: str,
    db_path: Path = DB_PATH,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    if field not in ALLOWED_LEAD_CONTACT_UPDATE_FIELDS:
        raise ValueError(f"Unsupported contact field: {field}")
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError("Contact field value must not be empty")

    def _run(c: sqlite3.Connection) -> Dict[str, Any]:
        row = c.execute("SELECT id FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if not row:
            raise ValueError(f"Lead not found: {lead_id}")
        if field == "website":
            domain = extract_domain(cleaned)
            c.execute(
                "UPDATE leads SET website = ?, domain = ?, updated_at = ? WHERE id = ?",
                (cleaned, domain, _now(), lead_id),
            )
        else:
            c.execute(
                f"UPDATE leads SET {field} = ?, updated_at = ? WHERE id = ?",
                (cleaned, _now(), lead_id),
            )
        updated = c.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        return dict(updated)

    if conn is not None:
        return _run(conn)
    with get_conn(db_path) as c:
        return _run(c)


def _csv_cell(value: Any) -> Any:
    if value is None:
        return ""
    text = str(value)
    if text and text[0] in ("=", "+", "-", "@"):
        return "'" + text
    return text


def export_leads_csv(db_path: Path = DB_PATH) -> str:
    import csv
    import io

    leads = list_leads(db_path)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id", "company_name", "website", "company_email", "partner_tier", "services", "fit_score",
        "status", "people_count", "has_decision_maker", "last_interaction", "next_deadline",
    ])
    for l in leads:
        writer.writerow([
            _csv_cell(l.get("id")),
            _csv_cell(l.get("company_name")),
            _csv_cell(l.get("website")),
            _csv_cell(l.get("company_email")),
            _csv_cell(l.get("partner_tier")),
            _csv_cell(", ".join(l.get("services") or [])),
            _csv_cell(l.get("fit_score")),
            _csv_cell(l.get("status")),
            _csv_cell(l.get("people_count")),
            _csv_cell(l.get("has_decision_maker")),
            _csv_cell(l.get("last_interaction")),
            _csv_cell(l.get("next_deadline")),
        ])
    return output.getvalue()


def _research_job_datetime(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ResearchJobError("invalid_request_snapshot", "stored research-job timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ResearchJobError("invalid_request_snapshot", "stored research-job timestamp is not timezone-aware")
    return parsed.astimezone(timezone.utc)


def _research_job_record(row: sqlite3.Row) -> ResearchJobRecord:
    try:
        snapshot_json = str(row["request_snapshot_json"])
        snapshot = DiscoveryRequest.model_validate(json.loads(snapshot_json))
        target_roles = tuple(json.loads(row["target_roles_json"]))
        source_types = tuple(json.loads(row["approved_source_types_json"]))
        return ResearchJobRecord(
            id=int(row["id"]),
            lead_id=int(row["lead_id"]),
            adapter_key=str(row["adapter_key"]),
            intent_key=str(row["intent_key"]),
            request_job_id=str(row["request_job_id"]),
            request_snapshot=snapshot,
            request_snapshot_json=snapshot_json,
            target_roles=target_roles,
            approved_source_types=source_types,
            status=ResearchJobStatus(row["status"]),
            priority=int(row["priority"]),
            requested_result_limit=int(row["requested_result_limit"]),
            max_pages=int(row["max_pages"]),
            max_requests=int(row["max_requests"]),
            timeout_seconds=int(row["timeout_seconds"]),
            max_attempts=int(row["max_attempts"]),
            attempt_count=int(row["attempt_count"]),
            not_before=_research_job_datetime(row["not_before"]),
            requested_by=str(row["requested_by"]),
            correlation_id=str(row["correlation_id"]),
            provider_config_ref=str(row["provider_config_ref"]),
            claimed_by=row["claimed_by"],
            lease_token=row["lease_token"],
            claimed_at=_research_job_datetime(row["claimed_at"]),
            lease_expires_at=_research_job_datetime(row["lease_expires_at"]),
            started_at=_research_job_datetime(row["started_at"]),
            completed_at=_research_job_datetime(row["completed_at"]),
            result_summary_json=row["result_summary_json"],
            safe_error_code=row["safe_error_code"],
            retry_after=_research_job_datetime(row["retry_after"]),
            version=int(row["version"]),
            created_at=_research_job_datetime(row["created_at"]),
            updated_at=_research_job_datetime(row["updated_at"]),
        )
    except ResearchJobError:
        raise
    except Exception as exc:
        raise ResearchJobError("invalid_request_snapshot", "stored research-job data is invalid") from exc


def _is_active_intent_unique_conflict(exc: sqlite3.IntegrityError) -> bool:
    """Recognize only the research_jobs.intent_key active uniqueness boundary."""
    if getattr(exc, "sqlite_errorcode", None) != sqlite3.SQLITE_CONSTRAINT_UNIQUE:
        return False
    if getattr(exc, "sqlite_errorname", None) != "SQLITE_CONSTRAINT_UNIQUE":
        return False
    return str(exc).strip() == "UNIQUE constraint failed: research_jobs.intent_key"


def enqueue_research_job(
    job: ResearchJobCreate,
    db_path: Path = DB_PATH,
    conn: Optional[sqlite3.Connection] = None,
) -> ResearchJobRecord:
    if not isinstance(job, ResearchJobCreate):
        raise ResearchJobError("invalid_request", "enqueue requires a validated ResearchJobCreate")
    snapshot_json = canonical_request_json(job.request)
    intent_key = research_intent_key(job)
    roles_json = json.dumps(sorted(set(job.request.target_roles)), separators=(",", ":"))
    source_types_json = json.dumps(sorted(set(job.request.approved_source_types)), separators=(",", ":"))

    def _run(c: sqlite3.Connection) -> ResearchJobRecord:
        lead = c.execute("SELECT id FROM leads WHERE id = ?", (job.request.lead_id,)).fetchone()
        if lead is None:
            raise ResearchJobError("lead_not_found", "lead not found")
        now = _now()
        values = (
            job.request.lead_id,
            job.adapter_key,
            intent_key,
            job.request.job_id,
            snapshot_json,
            roles_json,
            source_types_json,
            ResearchJobStatus.QUEUED.value,
            job.priority,
            job.request.result_limit,
            job.request.max_pages,
            job.request.max_requests,
            job.request.timeout_seconds,
            job.max_attempts,
            0,
            job.not_before.isoformat() if job.not_before else None,
            job.request.requester_identity,
            job.request.correlation_id,
            job.request.provider_config_ref,
            1,
            now,
            now,
        )
        try:
            cursor = c.execute(
                """INSERT INTO research_jobs (
                    lead_id, adapter_key, intent_key, request_job_id,
                    request_snapshot_json, target_roles_json, approved_source_types_json,
                    status, priority, requested_result_limit, max_pages, max_requests,
                    timeout_seconds, max_attempts, attempt_count, not_before,
                    requested_by, correlation_id, provider_config_ref, version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                values,
            )
            row = c.execute("SELECT * FROM research_jobs WHERE id = ?", (cursor.lastrowid,)).fetchone()
            return _research_job_record(row)
        except sqlite3.IntegrityError as exc:
            if not _is_active_intent_unique_conflict(exc):
                raise ResearchJobError("enqueue_persistence_failure", "research job could not be enqueued") from exc
            existing = c.execute(
                """SELECT * FROM research_jobs
                   WHERE intent_key = ? AND status IN ('queued', 'claimed', 'running', 'retry_wait')
                   ORDER BY id LIMIT 1""",
                (intent_key,),
            ).fetchone()
            if existing is not None:
                return _research_job_record(existing)
            raise ResearchJobError("enqueue_persistence_failure", "research job could not be enqueued")

    try:
        if conn is not None:
            return _run(conn)
        with get_conn(db_path) as c:
            return _run(c)
    except ResearchJobError:
        raise
    except sqlite3.Error as exc:
        raise ResearchJobError("enqueue_persistence_failure", "research job could not be enqueued") from exc


def get_research_job(
    job_id: int,
    db_path: Path = DB_PATH,
    conn: Optional[sqlite3.Connection] = None,
) -> ResearchJobRecord:
    if not isinstance(job_id, int) or isinstance(job_id, bool) or job_id <= 0:
        raise ResearchJobError("job_not_found", "research job not found")

    def _run(c: sqlite3.Connection) -> ResearchJobRecord:
        row = c.execute("SELECT * FROM research_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise ResearchJobError("job_not_found", "research job not found")
        return _research_job_record(row)

    if conn is not None:
        return _run(conn)
    with get_conn(db_path) as c:
        return _run(c)


def _research_job_filter(
    *,
    lead_id: Optional[int],
    status: Optional[ResearchJobStatus | str],
    adapter_key: Optional[str],
    limit: int,
) -> ResearchJobListFilter:
    try:
        return ResearchJobListFilter(lead_id=lead_id, status=status, adapter_key=adapter_key, limit=limit)
    except Exception as exc:
        if status is not None:
            raise ResearchJobError("invalid_status_filter", "invalid research-job status filter") from exc
        if adapter_key is not None and adapter_key not in ADAPTER_KEYS:
            raise ResearchJobError("unsupported_adapter", "unsupported research adapter") from exc
        raise ResearchJobError("invalid_request", "invalid research-job list filter") from exc


def list_research_jobs(
    *,
    lead_id: Optional[int] = None,
    status: Optional[ResearchJobStatus | str] = None,
    adapter_key: Optional[str] = None,
    limit: int = 50,
    db_path: Path = DB_PATH,
    conn: Optional[sqlite3.Connection] = None,
) -> list[ResearchJobRecord]:
    filters = _research_job_filter(lead_id=lead_id, status=status, adapter_key=adapter_key, limit=limit)

    def _run(c: sqlite3.Connection) -> list[ResearchJobRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if filters.lead_id is not None:
            clauses.append("lead_id = ?")
            params.append(filters.lead_id)
        if filters.status is not None:
            clauses.append("status = ?")
            params.append(filters.status.value)
        if filters.adapter_key is not None:
            clauses.append("adapter_key = ?")
            params.append(filters.adapter_key)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = c.execute(
            f"SELECT * FROM research_jobs {where} ORDER BY priority DESC, created_at ASC, id ASC LIMIT ?",
            (*params, filters.limit),
        ).fetchall()
        return [_research_job_record(row) for row in rows]

    if conn is not None:
        return _run(conn)
    with get_conn(db_path) as c:
        return _run(c)


def list_research_jobs_for_lead(
    lead_id: int,
    *,
    status: Optional[ResearchJobStatus | str] = None,
    adapter_key: Optional[str] = None,
    limit: int = 50,
    db_path: Path = DB_PATH,
    conn: Optional[sqlite3.Connection] = None,
) -> list[ResearchJobRecord]:
    return list_research_jobs(
        lead_id=lead_id,
        status=status,
        adapter_key=adapter_key,
        limit=limit,
        db_path=db_path,
        conn=conn,
    )


def _validate_worker_id(worker_id: str) -> str:
    if not isinstance(worker_id, str):
        raise ResearchJobError("invalid_worker_id", "worker identity is invalid")
    normalized = worker_id.strip()
    if not normalized or len(normalized) > MAX_WORKER_ID_LENGTH or any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise ResearchJobError("invalid_worker_id", "worker identity is invalid")
    return normalized


def _validate_lease_seconds(lease_seconds: int) -> int:
    if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not MIN_LEASE_SECONDS <= lease_seconds <= MAX_LEASE_SECONDS:
        raise ResearchJobError("invalid_lease_duration", "lease duration is invalid")
    return lease_seconds


def _owned_immediate_transaction(
    db_path: Path,
    conn: Optional[sqlite3.Connection],
    operation,
    failure_code: str,
):
    """Run one short write transaction without nesting a caller transaction."""
    if conn is not None:
        if conn.in_transaction:
            raise ResearchJobError(failure_code, "research-job transaction ownership is invalid")
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = operation(conn)
            conn.commit()
            return result
        except ResearchJobError:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise ResearchJobError(failure_code, "research-job persistence failed") from exc

    try:
        with get_conn(db_path) as owned:
            owned.execute("BEGIN IMMEDIATE")
            return operation(owned)
    except ResearchJobError:
        raise
    except sqlite3.Error as exc:
        raise ResearchJobError(failure_code, "research-job persistence failed") from exc


def claim_next_research_job(
    *,
    worker_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    db_path: Path = DB_PATH,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[ResearchJobRecord]:
    normalized_worker = _validate_worker_id(worker_id)
    duration = _validate_lease_seconds(lease_seconds)

    def _run(c: sqlite3.Connection) -> Optional[ResearchJobRecord]:
        claimed_at = datetime.now(timezone.utc)
        claimed_at_text = claimed_at.isoformat()
        lease_expires_text = (claimed_at + timedelta(seconds=duration)).isoformat()
        row = c.execute(
            """SELECT * FROM research_jobs
               WHERE status IN ('queued', 'retry_wait')
                 AND (not_before IS NULL OR not_before <= ?)
                 AND attempt_count < max_attempts
                 AND lease_token IS NULL
                 AND claimed_by IS NULL
                 AND lease_expires_at IS NULL
               ORDER BY priority DESC,
                        CASE WHEN not_before IS NULL THEN 0 ELSE 1 END ASC,
                        not_before ASC,
                        created_at ASC,
                        id ASC
               LIMIT 1""",
            (claimed_at_text,),
        ).fetchone()
        if row is None:
            return None
        result = c.execute(
            """UPDATE research_jobs
               SET status = 'claimed', claimed_by = ?, lease_token = ?,
                   claimed_at = ?, lease_expires_at = ?, attempt_count = attempt_count + 1,
                   version = version + 1, updated_at = ?
               WHERE id = ?
                 AND status IN ('queued', 'retry_wait')
                 AND (not_before IS NULL OR not_before <= ?)
                 AND attempt_count < max_attempts
                 AND lease_token IS NULL
                 AND claimed_by IS NULL
                 AND lease_expires_at IS NULL
                 AND version = ?""",
            (
                normalized_worker,
                secrets.token_urlsafe(32),
                claimed_at_text,
                lease_expires_text,
                claimed_at_text,
                row["id"],
                claimed_at_text,
                row["version"],
            ),
        )
        if result.rowcount != 1:
            raise ResearchJobError("claim_persistence_failure", "research job claim failed")
        claimed = c.execute("SELECT * FROM research_jobs WHERE id = ?", (row["id"],)).fetchone()
        return _research_job_record(claimed)

    return _owned_immediate_transaction(db_path, conn, _run, "claim_persistence_failure")


def _lease_guard(
    row: Optional[sqlite3.Row],
    *,
    lease_token: str,
    expected_version: int,
    allowed_states: tuple[str, ...],
    now: datetime,
) -> None:
    if row is None:
        raise ResearchJobError("job_not_found", "research job not found")
    if row["status"] not in allowed_states:
        raise ResearchJobError("invalid_claim_state", "research job state is not eligible")
    if row["version"] != expected_version:
        raise ResearchJobError("stale_job_version", "research job version is stale")
    if not isinstance(lease_token, str) or not lease_token or lease_token != row["lease_token"]:
        raise ResearchJobError("lease_token_mismatch", "research job lease token is invalid")
    expiry = _research_job_datetime(row["lease_expires_at"])
    if expiry is None or expiry <= now:
        raise ResearchJobError("lease_expired", "research job lease has expired")


def mark_research_job_running(
    job_id: int,
    *,
    lease_token: str,
    expected_version: int,
    db_path: Path = DB_PATH,
    conn: Optional[sqlite3.Connection] = None,
) -> ResearchJobRecord:
    def _run(c: sqlite3.Connection) -> ResearchJobRecord:
        now = datetime.now(timezone.utc)
        row = c.execute("SELECT * FROM research_jobs WHERE id = ?", (job_id,)).fetchone()
        _lease_guard(row, lease_token=lease_token, expected_version=expected_version, allowed_states=("claimed",), now=now)
        result = c.execute(
            """UPDATE research_jobs
               SET status = 'running', started_at = ?, version = version + 1, updated_at = ?
               WHERE id = ? AND status = 'claimed' AND lease_token = ?
                 AND version = ? AND lease_expires_at > ?""",
            (now.isoformat(), now.isoformat(), job_id, lease_token, expected_version, now.isoformat()),
        )
        if result.rowcount != 1:
            raise ResearchJobError("lease_update_persistence_failure", "research job state update failed")
        return _research_job_record(c.execute("SELECT * FROM research_jobs WHERE id = ?", (job_id,)).fetchone())

    return _owned_immediate_transaction(db_path, conn, _run, "lease_update_persistence_failure")


def renew_research_job_lease(
    job_id: int,
    *,
    lease_token: str,
    expected_version: int,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    db_path: Path = DB_PATH,
    conn: Optional[sqlite3.Connection] = None,
) -> ResearchJobRecord:
    duration = _validate_lease_seconds(lease_seconds)

    def _run(c: sqlite3.Connection) -> ResearchJobRecord:
        now = datetime.now(timezone.utc)
        row = c.execute("SELECT * FROM research_jobs WHERE id = ?", (job_id,)).fetchone()
        _lease_guard(row, lease_token=lease_token, expected_version=expected_version, allowed_states=("claimed", "running"), now=now)
        expiry = (now + timedelta(seconds=duration)).isoformat()
        result = c.execute(
            """UPDATE research_jobs
               SET lease_expires_at = ?, version = version + 1, updated_at = ?
               WHERE id = ? AND status IN ('claimed', 'running') AND lease_token = ?
                 AND version = ? AND lease_expires_at > ?""",
            (expiry, now.isoformat(), job_id, lease_token, expected_version, now.isoformat()),
        )
        if result.rowcount != 1:
            raise ResearchJobError("lease_update_persistence_failure", "research job lease update failed")
        return _research_job_record(c.execute("SELECT * FROM research_jobs WHERE id = ?", (job_id,)).fetchone())

    return _owned_immediate_transaction(db_path, conn, _run, "lease_update_persistence_failure")
