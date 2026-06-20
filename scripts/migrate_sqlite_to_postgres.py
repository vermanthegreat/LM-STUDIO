#!/usr/bin/env python3
"""Migrate legacy SQLite leads data into PostgreSQL contact schema."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import db as sqlite_db
from persistence.models import (
    ContactMethod,
    Extraction,
    Interaction,
    Organization,
    Person,
    Source,
    Task,
)
from persistence.session import get_engine, init_schema
from sqlalchemy import select
from sqlalchemy.orm import Session


@dataclass
class MigrationReport:
    migrated_organizations: int = 0
    migrated_people: int = 0
    migrated_sources: int = 0
    migrated_contact_methods: int = 0
    migrated_interactions: int = 0
    migrated_tasks: int = 0
    skipped: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "migrated_organizations": self.migrated_organizations,
            "migrated_people": self.migrated_people,
            "migrated_sources": self.migrated_sources,
            "migrated_contact_methods": self.migrated_contact_methods,
            "migrated_interactions": self.migrated_interactions,
            "migrated_tasks": self.migrated_tasks,
            "skipped": self.skipped,
            "conflicts": self.conflicts,
        }


def _load_sqlite_rows(db_path: Path) -> Dict[str, List[Dict[str, Any]]]:
    sqlite_db.init_db(db_path)
    with sqlite_db.get_conn(db_path) as conn:
        leads = [dict(r) for r in conn.execute("SELECT * FROM leads").fetchall()]
        people = [dict(r) for r in conn.execute("SELECT * FROM people").fetchall()]
        raw_sources = [dict(r) for r in conn.execute("SELECT * FROM raw_sources").fetchall()]
        interactions = [dict(r) for r in conn.execute("SELECT * FROM interactions").fetchall()]
        tasks = [dict(r) for r in conn.execute("SELECT * FROM tasks").fetchall()]
    return {
        "leads": leads,
        "people": people,
        "raw_sources": raw_sources,
        "interactions": interactions,
        "tasks": tasks,
    }


def _org_metadata(lead: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "partner_tier": lead.get("partner_tier"),
        "plus_partner_signal": bool(lead.get("plus_partner_signal")),
        "rating": lead.get("rating"),
        "review_count": lead.get("review_count"),
        "partner_since": lead.get("partner_since"),
        "primary_location": lead.get("primary_location"),
        "supported_locations": sqlite_db._json_loads(lead.get("supported_locations_json"), []),
        "languages": sqlite_db._json_loads(lead.get("languages_json"), []),
        "featured_work": sqlite_db._json_loads(lead.get("featured_work_json"), []),
        "services": sqlite_db._json_loads(lead.get("services_json"), []),
        "locations": sqlite_db._json_loads(lead.get("locations_json"), []),
        "industries": sqlite_db._json_loads(lead.get("industries_json"), []),
        "confidence": lead.get("confidence"),
        "extraction_status": lead.get("extraction_status"),
        "possible_duplicate": bool(lead.get("possible_duplicate")),
    }


def migrate(
    sqlite_path: Path,
    database_url: str,
    *,
    dry_run: bool = False,
) -> MigrationReport:
    report = MigrationReport()
    rows = _load_sqlite_rows(sqlite_path)
    if not dry_run:
        init_schema(database_url)

    engine = get_engine(database_url)
    session = Session(bind=engine)
    org_by_legacy: Dict[int, Organization] = {}
    person_by_legacy: Dict[int, Person] = {}

    try:
        for lead in rows["leads"]:
            legacy_id = lead.get("id")
            if legacy_id is None:
                report.skipped.append("lead:missing_id")
                continue
            existing = session.scalar(
                select(Organization).where(Organization.legacy_lead_id == legacy_id)
            )
            if existing:
                report.conflicts.append(f"organization:legacy_lead_id:{legacy_id}")
                org_by_legacy[legacy_id] = existing
                continue
            org = Organization(
                legacy_lead_id=legacy_id,
                name=lead.get("company_name"),
                normalized_name=lead.get("normalized_name"),
                website=lead.get("website"),
                normalized_domain=lead.get("domain"),
                description=lead.get("description"),
                status=lead.get("status") or "new",
                relevance_score=int(lead.get("fit_score") or 0),
                legacy_metadata=_org_metadata(lead),
            )
            session.add(org)
            session.flush()
            org_by_legacy[legacy_id] = org
            report.migrated_organizations += 1

            if lead.get("company_email"):
                session.add(
                    ContactMethod(
                        organization_id=org.id,
                        kind="email",
                        value=lead["company_email"],
                        normalized_value=sqlite_db.normalize_email(lead["company_email"]),
                        verification_status="unverified",
                    )
                )
                report.migrated_contact_methods += 1
            if lead.get("company_phone"):
                session.add(
                    ContactMethod(
                        organization_id=org.id,
                        kind="phone",
                        value=lead["company_phone"],
                        normalized_value=str(lead["company_phone"]).strip(),
                        verification_status="unverified",
                    )
                )
                report.migrated_contact_methods += 1

        for person_row in rows["people"]:
            legacy_person_id = person_row.get("id")
            lead_id = person_row.get("lead_id")
            org = org_by_legacy.get(lead_id)
            if not org:
                report.skipped.append(f"person:{legacy_person_id}:missing_org")
                continue
            person = Person(
                organization_id=org.id,
                name=person_row.get("name"),
                normalized_name=sqlite_db.normalize_name(person_row.get("name")),
                title=person_row.get("title"),
                is_decision_maker=bool(person_row.get("is_decision_maker")),
                relevance_reason=person_row.get("relevance_reason"),
                legacy_metadata={
                    "legacy_person_id": legacy_person_id,
                    "department": person_row.get("department"),
                    "seniority": person_row.get("seniority"),
                    "is_relevant_contact": person_row.get("is_relevant_contact"),
                    "confidence": person_row.get("confidence"),
                    "raw_source_id": person_row.get("raw_source_id"),
                },
            )
            session.add(person)
            session.flush()
            person_by_legacy[legacy_person_id] = person
            report.migrated_people += 1
            if person_row.get("email"):
                session.add(
                    ContactMethod(
                        person_id=person.id,
                        kind="email",
                        value=person_row["email"],
                        normalized_value=sqlite_db.normalize_email(person_row["email"]),
                        verification_status="unverified",
                    )
                )
                report.migrated_contact_methods += 1
            if person_row.get("linkedin_url"):
                session.add(
                    ContactMethod(
                        person_id=person.id,
                        kind="linkedin",
                        value=person_row["linkedin_url"],
                        normalized_value=sqlite_db.normalize_linkedin_url(person_row["linkedin_url"]),
                        verification_status="unverified",
                    )
                )
                report.migrated_contact_methods += 1

        for raw in rows["raw_sources"]:
            legacy_source_id = raw.get("id")
            lead_id = raw.get("lead_id")
            org = org_by_legacy.get(lead_id) if lead_id else None
            raw_text = raw.get("raw_text") or ""
            if not raw_text.strip():
                report.skipped.append(f"source:{legacy_source_id}:empty_text")
                continue
            existing = session.scalar(
                select(Source).where(Source.legacy_source_id == legacy_source_id)
            )
            if existing:
                report.conflicts.append(f"source:legacy_source_id:{legacy_source_id}")
                continue
            source = Source(
                organization_id=org.id if org else None,
                source_type=raw.get("source_type") or "note",
                source_url=raw.get("source_url"),
                raw_text=raw_text,
                content_hash=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
                captured_at=datetime.now(timezone.utc),
                legacy_source_id=legacy_source_id,
                legacy_metadata={
                    "lead_id": lead_id,
                    "source_filter_tier": raw.get("source_filter_tier"),
                    "parsed_json": sqlite_db._json_loads(raw.get("parsed_json")),
                    "extraction_status": raw.get("extraction_status"),
                    "confidence": raw.get("confidence"),
                },
            )
            session.add(source)
            session.flush()
            report.migrated_sources += 1
            extraction_status = raw.get("extraction_status") or "ok"
            mapped_status = "approved" if extraction_status in ("ok", "fallback") else "proposed"
            extraction = Extraction(
                source_id=source.id,
                status=mapped_status,
                confidence=float(raw.get("confidence") or 0.0),
                structured_output=sqlite_db._json_loads(raw.get("parsed_json")),
                approved_at=datetime.now(timezone.utc) if mapped_status == "approved" else None,
            )
            session.add(extraction)

        for item in rows["interactions"]:
            org = org_by_legacy.get(item.get("lead_id"))
            if not org:
                report.skipped.append(f"interaction:{item.get('id')}:missing_org")
                continue
            session.add(
                Interaction(
                    organization_id=org.id,
                    kind=item.get("type"),
                    summary=item.get("summary"),
                    requires_followup=bool(item.get("reply_needed")),
                    legacy_interaction_id=item.get("id"),
                    legacy_metadata={
                        "subject": item.get("subject"),
                        "body": item.get("body"),
                        "deadline": item.get("deadline"),
                        "priority": item.get("priority"),
                        "next_action": item.get("next_action"),
                        "status": item.get("status"),
                        "raw_source_id": item.get("raw_source_id"),
                        "person_id": item.get("person_id"),
                    },
                )
            )
            report.migrated_interactions += 1

        for item in rows["tasks"]:
            org = org_by_legacy.get(item.get("lead_id"))
            if not org:
                report.skipped.append(f"task:{item.get('id')}:missing_org")
                continue
            due = item.get("due_date")
            due_at = datetime.fromisoformat(due) if due else None
            session.add(
                Task(
                    organization_id=org.id,
                    title=item.get("title"),
                    priority=item.get("priority"),
                    status=item.get("status") or "open",
                    due_at=due_at,
                    legacy_task_id=item.get("id"),
                    legacy_metadata={
                        "source_interaction_id": item.get("source_interaction_id"),
                        "person_id": item.get("person_id"),
                    },
                )
            )
            report.migrated_tasks += 1

        if dry_run:
            session.rollback()
        else:
            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate SQLite leads into PostgreSQL")
    parser.add_argument("--sqlite-path", type=Path, default=Path("leads.db"))
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report-json", type=Path)
    args = parser.parse_args(argv)

    if not args.sqlite_path.exists():
        print(f"SQLite database not found: {args.sqlite_path}", file=sys.stderr)
        return 1

    report = migrate(args.sqlite_path, args.database_url, dry_run=args.dry_run)
    payload = report.to_dict()
    print(json.dumps(payload, indent=2))
    if args.report_json:
        args.report_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
