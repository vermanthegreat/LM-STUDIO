"""SQLite-backed contact store delegating to db.py."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import db
from discovery_models import DiscoveryOutcome
from discovery_materialization_models import DiscoveryOutcomeMaterializationResult
from research_job_models import (
    ResearchJobCreate, ResearchJobFinalization, ResearchJobRecord, ResearchJobRetrySchedule, ResearchJobStatus,
)

_active_sqlite_tx: ContextVar[tuple[Path, Any] | None] = ContextVar("_active_sqlite_tx", default=None)


def get_active_sqlite_connection(database_path: Path) -> Any | None:
    active = _active_sqlite_tx.get()
    if active is not None:
        db_path, conn = active
        if db_path == database_path:
            return conn
    return None


class SqliteContactStore:
    backend = "sqlite"

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def _kwargs(self) -> dict[str, Any]:
        kw: dict[str, Any] = {"db_path": self.database_path}
        active = _active_sqlite_tx.get()
        if active is not None:
            db_path, conn = active
            if db_path == self.database_path:
                kw["conn"] = conn
        return kw

    def init_db(self) -> None:
        db.init_db(self.database_path)

    @contextmanager
    def transaction(self):
        with db.get_conn(self.database_path) as conn:
            token = _active_sqlite_tx.set((self.database_path, conn))
            try:
                yield
            finally:
                _active_sqlite_tx.reset(token)

    def get_all_leads_simple(self) -> List[Dict[str, Any]]:
        return db.get_all_leads_simple(**self._kwargs())

    def list_leads(self, research_only: bool = False) -> List[Dict[str, Any]]:
        return db.list_leads(research_only=research_only, **self._kwargs())

    def get_lead(self, lead_id: int) -> Optional[Dict[str, Any]]:
        return db.get_lead(lead_id, **self._kwargs())

    def export_leads_csv(self) -> str:
        return db.export_leads_csv(**self._kwargs())

    def count_potential_clients(self) -> int:
        return db.count_potential_clients(**self._kwargs())

    def get_top_leads(self, limit: int = 10) -> List[Dict[str, Any]]:
        return db.get_top_leads(limit, **self._kwargs())

    def get_leads_without_contacts(self) -> List[Dict[str, Any]]:
        return db.get_leads_without_contacts(**self._kwargs())

    def get_leads_without_email(self) -> List[Dict[str, Any]]:
        return db.get_leads_without_email(**self._kwargs())

    def get_contact_summary(self) -> Dict[str, int]:
        return db.get_contact_summary(**self._kwargs())

    def list_contact_emails(self, limit: int = 50) -> List[Dict[str, Any]]:
        return db.list_contact_emails(limit, **self._kwargs())

    def list_contact_method_records(self) -> List[Dict[str, Any]]:
        leads = {lead["id"]: lead for lead in db.list_leads(**self._kwargs())}
        records: List[Dict[str, Any]] = []
        for row in db.list_contact_emails(10_000, **self._kwargs()):
            lead = leads.get(row["lead_id"], {})
            records.append(
                {
                    "lead_id": row["lead_id"],
                    "company_name": row["company_name"],
                    "person_name": row.get("person_name"),
                    "kind": "email",
                    "value": row["email"],
                    "verification_status": "unverified",
                    "organization_status": lead.get("status"),
                    "fit_score": int(lead.get("fit_score") or 0),
                }
            )
        return records

    def get_followups_due(self, due_on_or_before: Optional[str] = None) -> List[Dict[str, Any]]:
        return db.get_followups_due(due_on_or_before=due_on_or_before, **self._kwargs())

    def search_lead_by_name(self, name: str) -> List[Dict[str, Any]]:
        return db.search_lead_by_name(name, **self._kwargs())

    def find_matching_leads(
        self,
        company_name: Optional[str] = None,
        website: Optional[str] = None,
        linkedin_url: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return db.find_matching_leads(company_name, website, linkedin_url, **self._kwargs())

    def find_company_identity_candidates(
        self,
        evidence_kind: str,
        value: str,
    ) -> List[Dict[str, Any]]:
        return db.find_company_identity_candidates(evidence_kind, value, **self._kwargs())

    def find_leads_by_email(self, email: str) -> List[Dict[str, Any]]:
        return db.find_leads_by_email(email, **self._kwargs())

    def find_exact_email_matches(self, email: str) -> List[Dict[str, Any]]:
        return db.find_exact_email_matches(email, **self._kwargs())

    def upsert_lead(
        self,
        data: Dict[str, Any],
        lead_id: Optional[int] = None,
    ) -> Tuple[Dict[str, Any], bool]:
        return db.upsert_lead(data, lead_id=lead_id, **self._kwargs())

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
        return db.create_raw_source(
            source_type,
            raw_text,
            source_url=source_url,
            source_filter_tier=source_filter_tier,
            parsed_json=parsed_json,
            extraction_status=extraction_status,
            confidence=confidence,
            lead_id=lead_id,
            **self._kwargs(),
        )

    def link_raw_source_to_lead(self, raw_source_id: int, lead_id: int) -> None:
        db.link_raw_source_to_lead(raw_source_id, lead_id, **self._kwargs())

    def create_or_reuse_person_candidate(self, lead_id: int, raw_source_id: int, **fields: Any) -> Dict[str, Any]:
        return db.create_or_reuse_person_candidate(lead_id, raw_source_id, **fields, **self._kwargs())

    def get_person_candidate(self, candidate_id: int) -> Optional[Dict[str, Any]]:
        return db.get_person_candidate(candidate_id, **self._kwargs())

    def list_person_candidates_for_lead(self, lead_id: int) -> List[Dict[str, Any]]:
        return db.list_person_candidates_for_lead(lead_id, **self._kwargs())

    def update_person_candidate_status(self, candidate_id: int, expected_version: int, target_status: str, applied_person_id: Optional[int] = None) -> Dict[str, Any]:
        return db.update_person_candidate_status(candidate_id, expected_version, target_status, applied_person_id, **self._kwargs())

    def create_or_reuse_contact_candidate(self, lead_id: int, raw_source_id: int, **fields: Any) -> Dict[str, Any]:
        return db.create_or_reuse_contact_candidate(lead_id, raw_source_id, **fields, **self._kwargs())

    def get_contact_candidate(self, candidate_id: int) -> Optional[Dict[str, Any]]:
        return db.get_contact_candidate(candidate_id, **self._kwargs())

    def list_contact_candidates_for_lead(self, lead_id: int) -> List[Dict[str, Any]]:
        return db.list_contact_candidates_for_lead(lead_id, **self._kwargs())

    def update_contact_candidate_status(self, candidate_id: int, expected_version: int, target_status: str, applied_contact_method_id: Optional[int] = None) -> Dict[str, Any]:
        return db.update_contact_candidate_status(candidate_id, expected_version, target_status, applied_contact_method_id, **self._kwargs())

    def add_person(
        self,
        lead_id: int,
        data: Dict[str, Any],
        raw_source_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        return db.add_person(lead_id, data, raw_source_id=raw_source_id, **self._kwargs())

    def add_interaction(
        self,
        lead_id: int,
        data: Dict[str, Any],
        raw_source_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        return db.add_interaction(lead_id, data, raw_source_id=raw_source_id, **self._kwargs())

    def add_task(self, lead_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        return db.add_task(lead_id, data, **self._kwargs())

    def update_lead_fit_score(self, lead_id: int, fit_score: int) -> None:
        db.update_lead_fit_score(lead_id, fit_score, **self._kwargs())

    def update_lead_contact_field(
        self,
        lead_id: int,
        field: str,
        value: str,
    ) -> Dict[str, Any]:
        return db.update_lead_contact_field(lead_id, field, value, **self._kwargs())

    def sanitize_company_name(self, name: Optional[str]) -> Optional[str]:
        return db.sanitize_company_name(name)

    def extract_domain(self, website: Optional[str]) -> Optional[str]:
        return db.extract_domain(website)

    def list_imported_email_messages(
        self,
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
        app_timezone: str = "UTC",
    ) -> tuple[List[Dict[str, Any]], int]:
        import gmail_db

        gmail_db.init_gmail_db(self.database_path)
        with db.get_conn(self.database_path) as conn:
            gmail_db.ensure_gmail_tables(conn)
            rows, total = gmail_db.list_gmail_messages(
                conn,
                intent=intent,
                marker=marker,
                direction=direction,
                link_status=link_status,
                lead_id=lead_id,
                person_id=person_id,
                since=since,
                limit=limit,
                offset=offset,
            )
        return [gmail_db.row_to_public_dict(row, app_timezone=app_timezone) for row in rows], total

    def get_imported_email_thread(
        self,
        external_thread_id: str,
        *,
        external_account: Optional[str] = None,
        app_timezone: str = "UTC",
    ) -> List[Dict[str, Any]]:
        import gmail_db

        gmail_db.init_gmail_db(self.database_path)
        with db.get_conn(self.database_path) as conn:
            gmail_db.ensure_gmail_tables(conn)
            rows = gmail_db.get_thread_messages(
                conn,
                external_thread_id=external_thread_id,
                external_account=external_account,
            )
        return [gmail_db.row_to_public_dict(row, app_timezone=app_timezone) for row in rows]

    def enqueue_research_job(self, job: ResearchJobCreate) -> ResearchJobRecord:
        return db.enqueue_research_job(job, **self._kwargs())

    def get_research_job(self, job_id: int) -> ResearchJobRecord:
        return db.get_research_job(job_id, **self._kwargs())

    def list_research_jobs(
        self,
        *,
        lead_id: Optional[int] = None,
        status: Optional[ResearchJobStatus | str] = None,
        adapter_key: Optional[str] = None,
        limit: int = 50,
    ) -> List[ResearchJobRecord]:
        return db.list_research_jobs(
            lead_id=lead_id,
            status=status,
            adapter_key=adapter_key,
            limit=limit,
            **self._kwargs(),
        )

    def list_research_jobs_for_lead(
        self,
        lead_id: int,
        *,
        status: Optional[ResearchJobStatus | str] = None,
        adapter_key: Optional[str] = None,
        limit: int = 50,
    ) -> List[ResearchJobRecord]:
        return db.list_research_jobs_for_lead(
            lead_id,
            status=status,
            adapter_key=adapter_key,
            limit=limit,
            **self._kwargs(),
        )

    def claim_next_research_job(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 120,
    ) -> Optional[ResearchJobRecord]:
        return db.claim_next_research_job(
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            **self._kwargs(),
        )

    def mark_research_job_running(
        self,
        job_id: int,
        *,
        lease_token: str,
        expected_version: int,
    ) -> ResearchJobRecord:
        return db.mark_research_job_running(
            job_id,
            lease_token=lease_token,
            expected_version=expected_version,
            **self._kwargs(),
        )

    def renew_research_job_lease(
        self,
        job_id: int,
        *,
        lease_token: str,
        expected_version: int,
        lease_seconds: int = 120,
    ) -> ResearchJobRecord:
        return db.renew_research_job_lease(
            job_id,
            lease_token=lease_token,
            expected_version=expected_version,
            lease_seconds=lease_seconds,
            **self._kwargs(),
        )

    def finalize_research_job(self, job_id: int, *, lease_token: str, expected_version: int, finalization: ResearchJobFinalization) -> ResearchJobRecord:
        return db.finalize_research_job(job_id, lease_token=lease_token, expected_version=expected_version, finalization=finalization, **self._kwargs())

    def schedule_research_job_retry(self, job_id: int, *, lease_token: str, expected_version: int, retry: ResearchJobRetrySchedule) -> ResearchJobRecord:
        return db.schedule_research_job_retry(job_id, lease_token=lease_token, expected_version=expected_version, retry=retry, **self._kwargs())

    def cancel_research_job(self, job_id: int, *, expected_version: int, reason_code: Optional[str] = None) -> ResearchJobRecord:
        return db.cancel_research_job(job_id, expected_version=expected_version, reason_code=reason_code, **self._kwargs())

    def recover_stale_research_jobs(self, *, limit: int = 20) -> tuple[ResearchJobRecord, ...]:
        return db.recover_stale_research_jobs(limit=limit, **self._kwargs())

    def materialize_discovery_outcome(
        self,
        *,
        research_job_id: int,
        lease_token: str,
        expected_version: int,
        outcome: DiscoveryOutcome,
    ) -> DiscoveryOutcomeMaterializationResult:
        return db.materialize_discovery_outcome(
            research_job_id=research_job_id,
            lease_token=lease_token,
            expected_version=expected_version,
            outcome=outcome,
            **self._kwargs(),
        )
