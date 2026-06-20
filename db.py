"""SQLite persistence for lead intelligence app."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

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
    relevance_reason TEXT,
    confidence REAL DEFAULT 0.0,
    raw_source_id INTEGER REFERENCES raw_sources(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
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
    }
    for col, col_type in lead_migrations.items():
        if col not in lead_cols:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {col_type}")

    people_cols = {row[1] for row in conn.execute("PRAGMA table_info(people)").fetchall()}
    if "email" not in people_cols:
        conn.execute("ALTER TABLE people ADD COLUMN email TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_people_email ON people(email)")


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
        if existing:
            c.execute(
                """UPDATE people SET name=COALESCE(?,name), title=COALESCE(?,title),
                   department=COALESCE(?,department), seniority=COALESCE(?,seniority),
                   email=COALESCE(?,email), is_decision_maker=?, is_relevant_contact=?,
                   relevance_reason=COALESCE(?,relevance_reason),
                   confidence=?, raw_source_id=COALESCE(?,raw_source_id)
                   WHERE id=?""",
                (
                    data.get("name"),
                    data.get("title"),
                    data.get("department"),
                    data.get("seniority"),
                    em,
                    int(data.get("is_decision_maker", 0)),
                    int(data.get("is_relevant_contact", 0)),
                    data.get("relevance_reason"),
                    data.get("confidence", 0.0),
                    raw_source_id,
                    existing["id"],
                ),
            )
            row = c.execute("SELECT * FROM people WHERE id = ?", (existing["id"],)).fetchone()
            return dict(row)
        cur = c.execute(
            """INSERT INTO people
               (lead_id, name, title, department, seniority, email, linkedin_url,
                is_decision_maker, is_relevant_contact, relevance_reason,
                confidence, raw_source_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                lead_id,
                data.get("name"),
                data.get("title"),
                data.get("department"),
                data.get("seniority"),
                em,
                li,
                int(data.get("is_decision_maker", 0)),
                int(data.get("is_relevant_contact", 0)),
                data.get("relevance_reason"),
                data.get("confidence", 0.0),
                raw_source_id,
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

    def _run(c: sqlite3.Connection) -> Dict[str, Any]:
        cur = c.execute(
            """INSERT INTO tasks
               (lead_id, person_id, title, due_date, priority, status,
                source_interaction_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                lead_id,
                data.get("person_id"),
                data.get("title"),
                data.get("due_date"),
                data.get("priority"),
                data.get("status", "open"),
                data.get("source_interaction_id"),
                now,
            ),
        )
        row = c.execute("SELECT * FROM tasks WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)

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


def list_leads(db_path: Path = DB_PATH) -> List[Dict[str, Any]]:
    sql = """
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
    ORDER BY l.fit_score DESC, l.updated_at DESC
    """
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
            "SELECT * FROM people WHERE lead_id = ? ORDER BY is_decision_maker DESC, name",
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


def get_followups_due(db_path: Path = DB_PATH) -> List[Dict[str, Any]]:
    now = _now()[:10]
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
    with get_conn(db_path) as conn:
        rows = conn.execute(sql, (now, now)).fetchall()
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
