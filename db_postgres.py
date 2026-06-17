from __future__ import annotations
import os
import json
from typing import Any, Dict, List, Optional
from pathlib import Path
from dotenv import load_dotenv
import psycopg
from psycopg.rows import dict_row

load_dotenv()
DATABASE_URL = os.getenv('DATABASE_URL')

if not DATABASE_URL:
    raise RuntimeError('DATABASE_URL not set in environment. Please copy .env.example to .env and edit it.')


def get_connection():
    """Return a new psycopg connection using dict_row factory."""
    conn = psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row)
    return conn


def init_db(schema_path: str = 'schema.sql') -> None:
    schema_file = Path(schema_path)
    if not schema_file.exists():
        raise FileNotFoundError(f"Schema file not found: {schema_path}")
    sql = schema_file.read_text(encoding='utf-8')
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)


def _normalize_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    return ' '.join(name.strip().lower().split())


def upsert_company(name: Optional[str], website: Optional[str] = None, industry: Optional[str] = None,
                   country: Optional[str] = None, status: Optional[str] = None, fit_score: Optional[int] = None,
                   extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    normalized = _normalize_name(name) or ''
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO companies (name, normalized_name, website, industry, country, status, fit_score, extra, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now())
                ON CONFLICT (normalized_name) DO UPDATE SET
                    name = COALESCE(EXCLUDED.name, companies.name),
                    website = COALESCE(EXCLUDED.website, companies.website),
                    industry = COALESCE(EXCLUDED.industry, companies.industry),
                    country = COALESCE(EXCLUDED.country, companies.country),
                    status = COALESCE(EXCLUDED.status, companies.status),
                    fit_score = COALESCE(EXCLUDED.fit_score, companies.fit_score),
                    extra = COALESCE(EXCLUDED.extra, companies.extra),
                    updated_at = now()
                RETURNING *;
                """,
                (name, normalized, website, industry, country, status, fit_score, json.dumps(extra) if extra else None)
            )
            row = cur.fetchone()
            return dict(row) if row else {}


def upsert_contact(full_name: Optional[str], email: Optional[str] = None, role: Optional[str] = None,
                   company_id: Optional[str] = None, linkedin_url: Optional[str] = None,
                   status: Optional[str] = None, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            if email:
                cur.execute(
                    """
                    INSERT INTO contacts (company_id, full_name, email, role, linkedin_url, status, extra, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s, now())
                    ON CONFLICT (email) DO UPDATE SET
                        company_id = COALESCE(EXCLUDED.company_id, contacts.company_id),
                        full_name = COALESCE(EXCLUDED.full_name, contacts.full_name),
                        role = COALESCE(EXCLUDED.role, contacts.role),
                        linkedin_url = COALESCE(EXCLUDED.linkedin_url, contacts.linkedin_url),
                        status = COALESCE(EXCLUDED.status, contacts.status),
                        extra = COALESCE(EXCLUDED.extra, contacts.extra),
                        updated_at = now()
                    RETURNING *;
                    """,
                    (company_id, full_name, email, role, linkedin_url, status, json.dumps(extra) if extra else None)
                )
            else:
                # No email unique key available; insert a new contact record
                cur.execute(
                    """
                    INSERT INTO contacts (company_id, full_name, email, role, linkedin_url, status, extra, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s, now())
                    RETURNING *;
                    """,
                    (company_id, full_name, None, role, linkedin_url, status, json.dumps(extra) if extra else None)
                )
            row = cur.fetchone()
            return dict(row) if row else {}


def create_source(raw_text: str, extracted_json: Optional[Dict[str, Any]] = None, source_type: str = 'raw') -> Dict[str, Any]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sources (source_type, raw_text, extracted_json)
                VALUES (%s,%s,%s)
                RETURNING *;
                """,
                (source_type, raw_text, json.dumps(extracted_json) if extracted_json else None)
            )
            row = cur.fetchone()
            return dict(row) if row else {}


def log_interaction(company_id: Optional[str], contact_id: Optional[str], kind: str, note: Optional[str]) -> Dict[str, Any]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO interactions (company_id, contact_id, kind, note)
                VALUES (%s,%s,%s,%s)
                RETURNING *;
                """,
                (company_id, contact_id, kind, note)
            )
            row = cur.fetchone()
            return dict(row) if row else {}


def create_email_draft(company_id: Optional[str], contact_id: Optional[str], subject: str, body: str, status: str = 'draft') -> Dict[str, Any]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO email_drafts (company_id, contact_id, subject, body, status, updated_at)
                VALUES (%s,%s,%s,%s,%s, now())
                RETURNING *;
                """,
                (company_id, contact_id, subject, body, status)
            )
            row = cur.fetchone()
            return dict(row) if row else {}


def search_companies(query: str, limit: int = 20) -> List[Dict[str, Any]]:
    like = f"%{query}%"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM companies
                WHERE name ILIKE %s OR normalized_name ILIKE %s OR website ILIKE %s
                ORDER BY fit_score DESC NULLS LAST, created_at DESC
                LIMIT %s;
                """,
                (like, like, like, limit)
            )
            rows = cur.fetchall()
            return [dict(r) for r in rows]


def search_contacts(query: str, limit: int = 20) -> List[Dict[str, Any]]:
    like = f"%{query}%"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM contacts
                WHERE full_name ILIKE %s OR email ILIKE %s OR role ILIKE %s
                ORDER BY created_at DESC
                LIMIT %s;
                """,
                (like, like, like, limit)
            )
            rows = cur.fetchall()
            return [dict(r) for r in rows]


def count_companies(*, exclude_statuses: tuple[str, ...] = ("closed", "archived")) -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS c FROM companies
                WHERE status IS NULL OR status NOT IN %s
                """,
                (exclude_statuses,),
            )
            row = cur.fetchone()
            return int(row["c"]) if row else 0


def top_companies(limit: int = 10) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 25))
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, website, industry, country, status, fit_score, created_at, updated_at
                FROM companies
                WHERE status IS NULL OR status NOT IN ('closed', 'archived')
                ORDER BY fit_score DESC NULLS LAST, updated_at DESC NULLS LAST
                LIMIT %s
                """,
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]


def companies_without_contacts(limit: int = 25) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 25))
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, c.name, c.website, c.industry, c.country, c.status, c.fit_score
                FROM companies c
                WHERE NOT EXISTS (
                    SELECT 1 FROM contacts ct WHERE ct.company_id = c.id
                )
                AND (c.status IS NULL OR c.status NOT IN ('closed', 'archived'))
                ORDER BY c.fit_score DESC NULLS LAST, c.updated_at DESC NULLS LAST
                LIMIT %s
                """,
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]


def get_company_summary(name: str) -> Dict[str, Any]:
    like = f"%{name.strip()}%"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM companies
                WHERE name ILIKE %s OR normalized_name ILIKE %s
                ORDER BY fit_score DESC NULLS LAST
                LIMIT 1
                """,
                (like, like),
            )
            company = cur.fetchone()
            if not company:
                return {}
            company = dict(company)
            cur.execute(
                """
                SELECT id, full_name, email, role, linkedin_url, status
                FROM contacts WHERE company_id = %s
                ORDER BY created_at DESC
                LIMIT 20
                """,
                (company["id"],),
            )
            contacts = [dict(r) for r in cur.fetchall()]
            cur.execute(
                """
                SELECT id, kind, note, created_at
                FROM interactions WHERE company_id = %s
                ORDER BY created_at DESC
                LIMIT 10
                """,
                (company["id"],),
            )
            interactions = [dict(r) for r in cur.fetchall()]
    return {"company": company, "contacts": contacts, "interactions": interactions}


def postgres_schema_hint() -> str:
    return (
        "Tables: companies(id, name, website, industry, country, status, fit_score), "
        "contacts(id, company_id, full_name, email, role), "
        "interactions(id, company_id, contact_id, kind, note), "
        "email_drafts, sources."
    )
