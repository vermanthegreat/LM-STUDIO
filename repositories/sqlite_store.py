"""SQLite-backed contact store delegating to db.py."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import db


class SqliteContactStore:
    backend = "sqlite"

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._conn = None

    def _kwargs(self) -> dict[str, Any]:
        kw: dict[str, Any] = {"db_path": self.database_path}
        if self._conn is not None:
            kw["conn"] = self._conn
        return kw

    def init_db(self) -> None:
        db.init_db(self.database_path)

    @contextmanager
    def transaction(self):
        with db.get_conn(self.database_path) as conn:
            self._conn = conn
            try:
                yield
            finally:
                self._conn = None

    def get_all_leads_simple(self) -> List[Dict[str, Any]]:
        return db.get_all_leads_simple(**self._kwargs())

    def list_leads(self) -> List[Dict[str, Any]]:
        return db.list_leads(**self._kwargs())

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

    def get_followups_due(self) -> List[Dict[str, Any]]:
        return db.get_followups_due(**self._kwargs())

    def search_lead_by_name(self, name: str) -> List[Dict[str, Any]]:
        return db.search_lead_by_name(name, **self._kwargs())

    def find_matching_leads(
        self,
        company_name: Optional[str] = None,
        website: Optional[str] = None,
        linkedin_url: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return db.find_matching_leads(company_name, website, linkedin_url, **self._kwargs())

    def find_leads_by_email(self, email: str) -> List[Dict[str, Any]]:
        return db.find_leads_by_email(email, **self._kwargs())

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
