"""PostgreSQL-backed contact store using SQLAlchemy repositories."""

from __future__ import annotations

import csv
import hashlib
import io
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

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
from persistence.session import get_session_factory, init_schema
from persistence.unit_of_work import UnitOfWork
from repositories.mapping import (
    organization_to_lead_detail,
    organization_to_lead_row,
    person_to_dict,
    source_to_dict,
)
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session, selectinload


class PostgresContactStore:
    backend = "postgresql"

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self._session: Optional[Session] = None

    def _active_session(self) -> Session:
        if self._session is not None:
            return self._session
        factory = get_session_factory(self.database_url)
        return factory()

    @contextmanager
    def transaction(self):
        with UnitOfWork(self.database_url) as uow:
            self._session = uow.session
            try:
                yield
            finally:
                self._session = None

    def init_db(self) -> None:
        init_schema(self.database_url)

    def _org_by_lead_id(self, session: Session, lead_id: int) -> Optional[Organization]:
        return session.scalar(
            select(Organization)
            .where(Organization.legacy_lead_id == lead_id)
            .options(
                selectinload(Organization.people).selectinload(Person.contact_methods),
                selectinload(Organization.contact_methods),
                selectinload(Organization.interactions),
                selectinload(Organization.tasks),
            )
        )

    def _source_by_legacy_id(self, session: Session, legacy_id: int) -> Optional[Source]:
        return session.scalar(select(Source).where(Source.legacy_source_id == legacy_id))

    def get_all_leads_simple(self) -> List[Dict[str, Any]]:
        session = self._active_session()
        try:
            rows = session.scalars(
                select(Organization).order_by(Organization.name)
            ).all()
            return [{"id": o.legacy_lead_id, "company_name": o.name} for o in rows]
        finally:
            if self._session is None:
                session.close()

    def list_leads(self) -> List[Dict[str, Any]]:
        session = self._active_session()
        try:
            orgs = session.scalars(
                select(Organization)
                .options(
                    selectinload(Organization.people),
                    selectinload(Organization.contact_methods),
                    selectinload(Organization.interactions),
                    selectinload(Organization.tasks),
                )
                .order_by(Organization.relevance_score.desc(), Organization.updated_at.desc())
            ).all()
            result = []
            for org in orgs:
                row = organization_to_lead_row(org)
                row["people_count"] = len(org.people)
                row["has_decision_maker"] = any(p.is_decision_maker for p in org.people)
                row["last_interaction"] = max(
                    (i.created_at.isoformat() for i in org.interactions if i.created_at),
                    default=None,
                )
                deadlines = [t.due_at for t in org.tasks if t.status == "open" and t.due_at]
                row["next_deadline"] = min(deadlines).isoformat()[:10] if deadlines else None
                result.append(row)
            return result
        finally:
            if self._session is None:
                session.close()

    def get_lead(self, lead_id: int) -> Optional[Dict[str, Any]]:
        session = self._active_session()
        try:
            org = self._org_by_lead_id(session, lead_id)
            if not org:
                return None
            sources = session.scalars(
                select(Source)
                .where(Source.organization_id == org.id)
                .order_by(Source.created_at.desc())
            ).all()
            org._loaded_sources = list(sources)  # type: ignore[attr-defined]
            return organization_to_lead_detail(org)
        finally:
            if self._session is None:
                session.close()

    def export_leads_csv(self) -> str:
        leads = self.list_leads()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "id", "company_name", "website", "company_email", "partner_tier", "services", "fit_score",
            "status", "people_count", "has_decision_maker", "last_interaction", "next_deadline",
        ])
        for lead in leads:
            writer.writerow([
                sqlite_db._csv_cell(lead.get("id")),
                sqlite_db._csv_cell(lead.get("company_name")),
                sqlite_db._csv_cell(lead.get("website")),
                sqlite_db._csv_cell(lead.get("company_email")),
                sqlite_db._csv_cell(lead.get("partner_tier")),
                sqlite_db._csv_cell(", ".join(lead.get("services") or [])),
                sqlite_db._csv_cell(lead.get("fit_score")),
                sqlite_db._csv_cell(lead.get("status")),
                sqlite_db._csv_cell(lead.get("people_count")),
                sqlite_db._csv_cell(lead.get("has_decision_maker")),
                sqlite_db._csv_cell(lead.get("last_interaction")),
                sqlite_db._csv_cell(lead.get("next_deadline")),
            ])
        return output.getvalue()

    def count_potential_clients(self) -> int:
        session = self._active_session()
        try:
            return session.scalar(
                select(func.count())
                .select_from(Organization)
                .where(Organization.status.notin_(("closed", "archived")))
            ) or 0
        finally:
            if self._session is None:
                session.close()

    def get_top_leads(self, limit: int = 10) -> List[Dict[str, Any]]:
        return self.list_leads()[:limit]

    def get_leads_without_contacts(self) -> List[Dict[str, Any]]:
        session = self._active_session()
        try:
            orgs = session.scalars(
                select(Organization)
                .where(~exists(select(Person.id).where(Person.organization_id == Organization.id)))
                .options(selectinload(Organization.contact_methods))
                .order_by(Organization.relevance_score.desc())
            ).all()
            return [organization_to_lead_row(o) for o in orgs]
        finally:
            if self._session is None:
                session.close()

    def get_leads_without_email(self) -> List[Dict[str, Any]]:
        session = self._active_session()
        try:
            orgs = session.scalars(
                select(Organization)
                .where(Organization.status.notin_(("closed", "archived")))
                .options(
                    selectinload(Organization.contact_methods),
                    selectinload(Organization.people).selectinload(Person.contact_methods),
                )
            ).all()
            result = []
            for org in orgs:
                if organization_to_lead_row(org).get("company_email"):
                    continue
                if any(person_to_dict(p).get("email") for p in org.people):
                    continue
                result.append(organization_to_lead_row(org))
            result.sort(key=lambda r: r.get("fit_score") or 0, reverse=True)
            return result
        finally:
            if self._session is None:
                session.close()

    def get_contact_summary(self) -> Dict[str, int]:
        session = self._active_session()
        try:
            active = Organization.status.notin_(("closed", "archived"))
            companies = session.scalar(
                select(func.count()).select_from(Organization).where(active)
            ) or 0
            with_company_email = 0
            with_people = session.scalar(
                select(func.count(func.distinct(Person.organization_id)))
                .join(Organization)
                .where(active)
            ) or 0
            with_person_email = 0
            with_any_email = 0
            orgs = session.scalars(
                select(Organization)
                .where(active)
                .options(
                    selectinload(Organization.contact_methods),
                    selectinload(Organization.people).selectinload(Person.contact_methods),
                )
            ).all()
            for org in orgs:
                row = organization_to_lead_row(org)
                has_company = bool(row.get("company_email"))
                has_person = any(person_to_dict(p).get("email") for p in org.people)
                if has_company:
                    with_company_email += 1
                if has_person:
                    with_person_email += 1
                if has_company or has_person:
                    with_any_email += 1
            email_interactions = session.scalar(
                select(func.count())
                .select_from(Interaction)
                .where(func.lower(Interaction.kind) == "email")
            ) or 0
            return {
                "companies": companies,
                "with_company_email": with_company_email,
                "with_people": with_people,
                "with_person_email": with_person_email,
                "with_any_email": with_any_email,
                "without_email": companies - with_any_email,
                "email_interactions": email_interactions,
            }
        finally:
            if self._session is None:
                session.close()

    def list_contact_emails(self, limit: int = 50) -> List[Dict[str, Any]]:
        session = self._active_session()
        try:
            rows: List[Dict[str, Any]] = []
            methods = session.scalars(
                select(ContactMethod)
                .where(ContactMethod.kind == "email")
                .order_by(ContactMethod.normalized_value)
            ).all()
            for method in methods:
                if method.organization_id:
                    org = session.get(Organization, method.organization_id)
                    if not org:
                        continue
                    rows.append({
                        "lead_id": org.legacy_lead_id,
                        "company_name": org.name,
                        "email": method.value,
                        "person_name": None,
                        "source": "company",
                    })
                elif method.person_id:
                    person = session.get(Person, method.person_id)
                    if not person or not person.organization:
                        continue
                    rows.append({
                        "lead_id": person.organization.legacy_lead_id,
                        "company_name": person.organization.name,
                        "email": method.value,
                        "person_name": person.name,
                        "source": "person",
                    })
            return rows[:limit]
        finally:
            if self._session is None:
                session.close()

    def get_followups_due(self) -> List[Dict[str, Any]]:
        session = self._active_session()
        try:
            today = datetime.now(timezone.utc).date()
            items: List[Dict[str, Any]] = []
            tasks = session.scalars(
                select(Task)
                .join(Organization)
                .where(Task.status == "open", Task.due_at.is_not(None))
            ).all()
            for task in tasks:
                if task.due_at and task.due_at.date() <= today and task.organization:
                    items.append({
                        "company_name": task.organization.name,
                        "lead_id": task.organization.legacy_lead_id,
                        "title": task.title,
                        "due_date": task.due_at.date().isoformat(),
                        "priority": task.priority,
                        "item_type": "task",
                    })
            interactions = session.scalars(
                select(Interaction)
                .join(Organization)
                .where(Interaction.requires_followup.is_(True))
            ).all()
            for item in interactions:
                meta = item.legacy_metadata or {}
                deadline = meta.get("deadline")
                if deadline and deadline <= today.isoformat() and item.organization:
                    items.append({
                        "company_name": item.organization.name,
                        "lead_id": item.organization.legacy_lead_id,
                        "subject": meta.get("subject"),
                        "due_date": deadline,
                        "priority": meta.get("priority"),
                        "item_type": "interaction",
                    })
            items.sort(key=lambda x: x.get("due_date") or "")
            return items
        finally:
            if self._session is None:
                session.close()

    def search_lead_by_name(self, name: str) -> List[Dict[str, Any]]:
        session = self._active_session()
        try:
            norm = sqlite_db.normalize_name(name)
            like = f"%{norm}%"
            orgs = session.scalars(
                select(Organization)
                .where(
                    or_(
                        Organization.normalized_name.ilike(like),
                        Organization.name.ilike(f"%{name}%"),
                    )
                )
                .options(selectinload(Organization.contact_methods))
            ).all()
            return [organization_to_lead_row(o) for o in orgs]
        finally:
            if self._session is None:
                session.close()

    def find_matching_leads(
        self,
        company_name: Optional[str] = None,
        website: Optional[str] = None,
        linkedin_url: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        session = self._active_session()
        try:
            clauses = []
            norm = sqlite_db.normalize_name(company_name)
            domain = sqlite_db.extract_domain(website)
            if norm:
                clauses.append(Organization.normalized_name == norm)
            if domain:
                clauses.append(Organization.normalized_domain == domain)
            if not clauses:
                return []
            orgs = session.scalars(
                select(Organization)
                .where(or_(*clauses))
                .options(selectinload(Organization.contact_methods))
            ).all()
            results = [organization_to_lead_row(o) for o in orgs]
            li = sqlite_db.normalize_linkedin_url(linkedin_url)
            if li:
                people = session.scalars(
                    select(Person).join(ContactMethod).where(
                        ContactMethod.kind == "linkedin",
                        ContactMethod.normalized_value == li,
                    )
                ).all()
                seen = {r["id"] for r in results}
                for person in people:
                    if person.organization and person.organization.legacy_lead_id not in seen:
                        results.append(organization_to_lead_row(person.organization))
            return results
        finally:
            if self._session is None:
                session.close()

    def find_leads_by_email(self, email: str) -> List[Dict[str, Any]]:
        norm = sqlite_db.normalize_email(email)
        if not norm:
            return []
        domain = sqlite_db.email_domain(norm)
        if not domain or domain in sqlite_db.PERSONAL_EMAIL_DOMAINS:
            return []
        session = self._active_session()
        try:
            orgs = session.scalars(
                select(Organization)
                .where(
                    or_(
                        func.lower(Organization.normalized_domain) == domain,
                        exists(
                            select(ContactMethod.id).where(
                                ContactMethod.organization_id == Organization.id,
                                ContactMethod.kind == "email",
                                func.lower(ContactMethod.normalized_value) == norm,
                            )
                        ),
                    )
                )
                .options(selectinload(Organization.contact_methods))
            ).all()
            return [organization_to_lead_row(o) for o in orgs]
        finally:
            if self._session is None:
                session.close()

    def upsert_lead(
        self,
        data: Dict[str, Any],
        lead_id: Optional[int] = None,
    ) -> Tuple[Dict[str, Any], bool]:
        session = self._active_session()
        own_session = self._session is None
        try:
            org = self._org_by_lead_id(session, lead_id) if lead_id else None
            matches = self.find_matching_leads(data.get("company_name"), data.get("website"))
            is_new = org is None
            if org is None and matches:
                org = self._org_by_lead_id(session, matches[0]["id"])
                is_new = False
            if org is None:
                org = Organization()
                session.add(org)
                is_new = True

            company_name = sqlite_db.sanitize_company_name(data.get("company_name"))
            org.name = company_name or org.name
            org.normalized_name = sqlite_db.normalize_name(company_name) or org.normalized_name
            org.website = data.get("website") or org.website
            org.normalized_domain = sqlite_db.extract_domain(org.website) or org.normalized_domain
            org.description = data.get("description") or org.description
            org.status = data.get("status") or org.status or "new"
            org.relevance_score = int(data.get("fit_score") or org.relevance_score or 0)

            meta = dict(org.legacy_metadata or {})
            for key in (
                "partner_tier", "plus_partner_signal", "rating", "review_count", "partner_since",
                "primary_location", "supported_locations", "languages", "featured_work",
                "services", "locations", "industries", "confidence", "extraction_status",
                "possible_duplicate",
            ):
                if key in data and data[key] is not None:
                    meta[key] = data[key]
            org.legacy_metadata = meta

            if data.get("company_email"):
                self._upsert_org_contact(session, org, "email", data["company_email"])
            if data.get("company_phone"):
                self._upsert_org_contact(session, org, "phone", data["company_phone"])

            if own_session:
                session.commit()
                session.refresh(org)
            else:
                session.flush()
                session.refresh(org)
            return organization_to_lead_row(org), is_new
        except Exception:
            if own_session:
                session.rollback()
            raise
        finally:
            if own_session:
                session.close()

    def _upsert_org_contact(
        self,
        session: Session,
        org: Organization,
        kind: str,
        value: str,
    ) -> None:
        norm = value.strip().lower()
        existing = session.scalar(
            select(ContactMethod).where(
                ContactMethod.organization_id == org.id,
                ContactMethod.kind == kind,
                ContactMethod.normalized_value == norm,
            )
        )
        if existing:
            existing.value = value
            return
        session.add(
            ContactMethod(
                organization_id=org.id,
                kind=kind,
                value=value,
                normalized_value=norm,
                verification_status="unverified",
            )
        )

    def create_raw_source(
        self,
        source_type: str,
        raw_text: str,
        source_url: Optional[str] = None,
        source_filter_tier: Optional[str] = None,
        parsed_json: Optional[Dict[str, Any]] = None,
        extraction_status: str = "ok",
        confidence: float = 0.0,
        lead_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        session = self._active_session()
        own_session = self._session is None
        try:
            org = self._org_by_lead_id(session, lead_id) if lead_id else None
            legacy_source_id = self._next_legacy_source_id(session)
            source = Source(
                organization_id=org.id if org else None,
                source_type=source_type,
                source_url=source_url,
                raw_text=raw_text,
                content_hash=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
                captured_at=datetime.now(timezone.utc),
                legacy_source_id=legacy_source_id,
                legacy_metadata={
                    "lead_id": lead_id,
                    "source_filter_tier": source_filter_tier,
                    "parsed_json": parsed_json,
                    "extraction_status": extraction_status,
                    "confidence": confidence,
                },
            )
            session.add(source)
            session.flush()

            extraction = Extraction(
                source_id=source.id,
                status="proposed",
                confidence=confidence,
                structured_output=parsed_json,
            )
            session.add(extraction)
            session.flush()
            self._approve_extraction(session, extraction, org)

            if own_session:
                session.commit()
            return source_to_dict(source)
        except Exception:
            if own_session:
                session.rollback()
            raise
        finally:
            if own_session:
                session.close()

    def _approve_extraction(
        self,
        session: Session,
        extraction: Extraction,
        org: Optional[Organization],
    ) -> None:
        extraction.status = "approved"
        extraction.approved_at = datetime.now(timezone.utc)

    def _next_legacy_source_id(self, session: Session) -> int:
        current = session.scalar(select(func.max(Source.legacy_source_id))) or 0
        return int(current) + 1

    def _next_legacy_interaction_id(self, session: Session) -> int:
        current = session.scalar(select(func.max(Interaction.legacy_interaction_id))) or 0
        return int(current) + 1

    def _next_legacy_task_id(self, session: Session) -> int:
        current = session.scalar(select(func.max(Task.legacy_task_id))) or 0
        return int(current) + 1

    def link_raw_source_to_lead(self, raw_source_id: int, lead_id: int) -> None:
        session = self._active_session()
        own_session = self._session is None
        try:
            source = self._source_by_legacy_id(session, raw_source_id)
            org = self._org_by_lead_id(session, lead_id)
            if source and org:
                source.organization_id = org.id
                meta = dict(source.legacy_metadata or {})
                meta["lead_id"] = lead_id
                source.legacy_metadata = meta
            if own_session:
                session.commit()
        except Exception:
            if own_session:
                session.rollback()
            raise
        finally:
            if own_session:
                session.close()

    def add_person(
        self,
        lead_id: int,
        data: Dict[str, Any],
        raw_source_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        session = self._active_session()
        own_session = self._session is None
        try:
            org = self._org_by_lead_id(session, lead_id)
            if not org:
                raise ValueError(f"Lead {lead_id} not found")
            person = Person(
                organization_id=org.id,
                name=data.get("name"),
                normalized_name=sqlite_db.normalize_name(data.get("name")),
                title=data.get("title"),
                is_decision_maker=bool(data.get("is_decision_maker")),
                relevance_reason=data.get("relevance_reason"),
                legacy_metadata={
                    "department": data.get("department"),
                    "seniority": data.get("seniority"),
                    "is_relevant_contact": data.get("is_relevant_contact", 0),
                    "confidence": data.get("confidence", 0.0),
                    "raw_source_id": raw_source_id,
                },
            )
            session.add(person)
            session.flush()
            if data.get("email"):
                session.add(
                    ContactMethod(
                        person_id=person.id,
                        kind="email",
                        value=data["email"],
                        normalized_value=sqlite_db.normalize_email(data["email"]),
                        verification_status="unverified",
                    )
                )
            if data.get("linkedin_url"):
                session.add(
                    ContactMethod(
                        person_id=person.id,
                        kind="linkedin",
                        value=data["linkedin_url"],
                        normalized_value=sqlite_db.normalize_linkedin_url(data["linkedin_url"]),
                        verification_status="unverified",
                    )
                )
            if own_session:
                session.commit()
                session.refresh(person)
            person.organization = org
            return person_to_dict(person)
        except Exception:
            if own_session:
                session.rollback()
            raise
        finally:
            if own_session:
                session.close()

    def add_interaction(
        self,
        lead_id: int,
        data: Dict[str, Any],
        raw_source_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        session = self._active_session()
        own_session = self._session is None
        try:
            org = self._org_by_lead_id(session, lead_id)
            if not org:
                raise ValueError(f"Lead {lead_id} not found")
            source = self._source_by_legacy_id(session, raw_source_id) if raw_source_id else None
            interaction = Interaction(
                organization_id=org.id,
                kind=data.get("type", "email"),
                summary=data.get("summary"),
                source_id=source.id if source else None,
                requires_followup=bool(data.get("reply_needed")),
                legacy_interaction_id=self._next_legacy_interaction_id(session),
                legacy_metadata={
                    "subject": data.get("subject"),
                    "body": data.get("body"),
                    "deadline": data.get("deadline"),
                    "priority": data.get("priority"),
                    "next_action": data.get("next_action"),
                    "status": data.get("status", "open"),
                    "raw_source_id": raw_source_id,
                    "person_id": data.get("person_id"),
                },
            )
            session.add(interaction)
            if own_session:
                session.commit()
                session.refresh(interaction)
            row = {
                "id": interaction.legacy_interaction_id,
                "lead_id": lead_id,
            }
            return row
        except Exception:
            if own_session:
                session.rollback()
            raise
        finally:
            if own_session:
                session.close()

    def add_task(self, lead_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        session = self._active_session()
        own_session = self._session is None
        try:
            org = self._org_by_lead_id(session, lead_id)
            if not org:
                raise ValueError(f"Lead {lead_id} not found")
            due = data.get("due_date")
            due_at = None
            if due:
                due_at = datetime.fromisoformat(str(due))
            task = Task(
                organization_id=org.id,
                title=data.get("title"),
                priority=data.get("priority"),
                status=data.get("status", "open"),
                due_at=due_at,
                legacy_task_id=self._next_legacy_task_id(session),
                legacy_metadata={
                    "source_interaction_id": data.get("source_interaction_id"),
                    "person_id": data.get("person_id"),
                },
            )
            session.add(task)
            if own_session:
                session.commit()
                session.refresh(task)
            return {"id": task.legacy_task_id, "lead_id": lead_id}
        except Exception:
            if own_session:
                session.rollback()
            raise
        finally:
            if own_session:
                session.close()

    def update_lead_fit_score(self, lead_id: int, fit_score: int) -> None:
        session = self._active_session()
        own_session = self._session is None
        try:
            org = self._org_by_lead_id(session, lead_id)
            if org:
                org.relevance_score = fit_score
            if own_session:
                session.commit()
        except Exception:
            if own_session:
                session.rollback()
            raise
        finally:
            if own_session:
                session.close()

    def sanitize_company_name(self, name: Optional[str]) -> Optional[str]:
        return sqlite_db.sanitize_company_name(name)

    def extract_domain(self, website: Optional[str]) -> Optional[str]:
        return sqlite_db.extract_domain(website)
