"""Repository protocol for contact persistence."""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable


@runtime_checkable
class ContactStore(Protocol):
    """Application-facing persistence contract (lead-shaped dicts for UI compatibility)."""

    backend: str

    def init_db(self) -> None: ...

    def transaction(self) -> AbstractContextManager[None]: ...

    def get_all_leads_simple(self) -> List[Dict[str, Any]]: ...

    def list_leads(self) -> List[Dict[str, Any]]: ...

    def get_lead(self, lead_id: int) -> Optional[Dict[str, Any]]: ...

    def export_leads_csv(self) -> str: ...

    def count_potential_clients(self) -> int: ...

    def get_top_leads(self, limit: int = 10) -> List[Dict[str, Any]]: ...

    def get_leads_without_contacts(self) -> List[Dict[str, Any]]: ...

    def get_leads_without_email(self) -> List[Dict[str, Any]]: ...

    def get_contact_summary(self) -> Dict[str, int]: ...

    def list_contact_emails(self, limit: int = 50) -> List[Dict[str, Any]]: ...

    def get_followups_due(self) -> List[Dict[str, Any]]: ...

    def search_lead_by_name(self, name: str) -> List[Dict[str, Any]]: ...

    def find_matching_leads(
        self,
        company_name: Optional[str] = None,
        website: Optional[str] = None,
        linkedin_url: Optional[str] = None,
    ) -> List[Dict[str, Any]]: ...

    def find_leads_by_email(self, email: str) -> List[Dict[str, Any]]: ...

    def upsert_lead(
        self,
        data: Dict[str, Any],
        lead_id: Optional[int] = None,
    ) -> Tuple[Dict[str, Any], bool]: ...

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
    ) -> Dict[str, Any]: ...

    def link_raw_source_to_lead(self, raw_source_id: int, lead_id: int) -> None: ...

    def add_person(
        self,
        lead_id: int,
        data: Dict[str, Any],
        raw_source_id: Optional[int] = None,
    ) -> Dict[str, Any]: ...

    def add_interaction(
        self,
        lead_id: int,
        data: Dict[str, Any],
        raw_source_id: Optional[int] = None,
    ) -> Dict[str, Any]: ...

    def add_task(self, lead_id: int, data: Dict[str, Any]) -> Dict[str, Any]: ...

    def update_lead_fit_score(self, lead_id: int, fit_score: int) -> None: ...

    def sanitize_company_name(self, name: Optional[str]) -> Optional[str]: ...

    def extract_domain(self, website: Optional[str]) -> Optional[str]: ...
