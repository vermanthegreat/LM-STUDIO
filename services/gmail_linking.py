"""Deterministic, evidence-ranked Gmail-to-CRM linking (no model involvement)."""
from __future__ import annotations

from dataclasses import dataclass, field
from email.utils import parseaddr
from typing import Any, Optional, Protocol, runtime_checkable

import db
from gmail_schemas import EmailDirection, LinkStatus, NormalizedGmailMessage
from services.email_classification_service import determine_direction

PUBLIC_EMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com", "icloud.com", "me.com", "proton.me", "protonmail.com", "aol.com", "gmx.com", "mail.com"})
LINK_STRENGTH = {"exact_person_email": 500, "exact_company_email": 400, "thread_inherited": 350, "unique_company_domain": 200, "unmatched": 0}


@runtime_checkable
class GmailLinkStore(Protocol):
    def find_exact_email_matches(self, email: str) -> list[dict[str, Any]]: ...
    def find_thread_links(self, *, external_account: str, external_thread_id: str, exclude_message_id: str) -> list[dict[str, Any]]: ...
    def find_exact_company_name_matches(self, company_name: str) -> list[dict[str, Any]]: ...
    def find_company_domain_matches(self, domain: str) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class LinkDecision:
    link_status: LinkStatus
    lead_id: Optional[int] = None
    person_id: Optional[int] = None
    reason: str = "unmatched"
    strength: int = 0
    evidence: dict[str, Any] = field(default_factory=dict)
    review_hint: Optional[str] = None


def normalize_counterparty_email(value: str | None) -> str | None:
    """Return a syntactically usable canonical address, never a display name."""
    _, address = parseaddr(value or "")
    normalized = address.strip().lower()
    if not normalized or normalized.count("@") != 1:
        return None
    local, domain = normalized.rsplit("@", 1)
    if not local or not domain or "." not in domain or any(ch.isspace() for ch in normalized):
        return None
    return normalized


def extract_counterparties(message: NormalizedGmailMessage, *, operator_aliases: set[str] | None = None) -> tuple[list[str], list[str]]:
    """Return sorted external addresses and safe validation warnings."""
    aliases = {item for item in (normalize_counterparty_email(a) for a in (operator_aliases or set()) | {message.account_email}) if item}
    direction = determine_direction(message)
    raw = [message.from_address.email] if direction == EmailDirection.INBOUND else [a.email for a in [*message.to_addresses, *message.cc_addresses]]
    result: set[str] = set(); warnings: list[str] = []
    for value in raw:
        email = normalize_counterparty_email(value)
        if not email:
            if value: warnings.append("malformed_counterparty_address")
        elif email not in aliases:
            result.add(email)
    return sorted(result), sorted(set(warnings))


def _ambiguous(reason: str, *, emails: list[str], people: set[int] = set(), leads: set[int] = set(), domain: str | None = None) -> LinkDecision:
    evidence: dict[str, Any] = {"candidate_lead_ids": sorted(leads), "candidate_person_ids": sorted(people), "matched_email": emails[0] if len(emails) == 1 else None, "matched_domain": domain, "reason": reason}
    return LinkDecision(LinkStatus.AMBIGUOUS, reason=reason, evidence=evidence, review_hint=reason)


def resolve_contact_link(store: GmailLinkStore, message: NormalizedGmailMessage, *, operator_aliases: set[str] | None = None) -> LinkDecision:
    # Partner Directory confirmations are transport evidence: only their explicit target
    # may be used, never the provider sender address.
    from services.email_classification_service import extract_shopify_partner_inquiry_target, is_shopify_partner_inquiry_confirmation
    if is_shopify_partner_inquiry_confirmation(message):
        target = extract_shopify_partner_inquiry_target(message) or ""
        matches = {int(m["lead_id"]) for m in store.find_exact_company_name_matches(target) if m.get("lead_id")}
        if len(matches) > 1:
            return _ambiguous("multiple_exact_matches", emails=[], leads=matches)
        if matches:
            return LinkDecision(LinkStatus.LINKED, next(iter(matches)), None, "exact_company_email", 400, {"partner_directory_target": target})
        return LinkDecision(LinkStatus.UNLINKED, reason="unmatched", evidence={"partner_directory_target": target})
    emails, warnings = extract_counterparties(message, operator_aliases=operator_aliases)
    thread = store.find_thread_links(external_account=message.account_email, external_thread_id=message.thread_id or message.message_id, exclude_message_id=message.message_id) if message.thread_id else []
    strong_leads = {int(r["lead_id"]) for r in thread if r.get("lead_id") and int(r.get("link_strength") or 0) >= 200}
    if len(strong_leads) > 1:
        return _ambiguous("conflicting_thread_links", emails=emails, leads=strong_leads)
    person_matches = [m for e in emails for m in store.find_exact_email_matches(e) if m.get("kind") == "person"]
    person_leads = {int(m["lead_id"]) for m in person_matches if m.get("lead_id")}; person_ids = {int(m["person_id"]) for m in person_matches if m.get("person_id")}
    if len(person_ids) > 1 or len(person_leads) > 1:
        return _ambiguous("multiple_exact_matches", emails=emails, people=person_ids, leads=person_leads)
    if person_matches:
        match = person_matches[0]; return LinkDecision(LinkStatus.LINKED, int(match["lead_id"]), int(match["person_id"]), "exact_person_email", 500, {"matched_email": emails[0], "counterparties": emails, "warnings": warnings})
    company_matches = [m for e in emails for m in store.find_exact_email_matches(e) if m.get("kind") == "organization"]
    company_leads = {int(m["lead_id"]) for m in company_matches if m.get("lead_id")}
    if len(company_leads) > 1:
        return _ambiguous("multiple_exact_matches", emails=emails, leads=company_leads)
    if company_leads:
        return LinkDecision(LinkStatus.LINKED, next(iter(company_leads)), None, "exact_company_email", 400, {"matched_email": emails[0], "counterparties": emails, "warnings": warnings})
    if strong_leads:
        lead = next(iter(strong_leads)); exact_people = {int(r["person_id"]) for r in thread if r.get("lead_id") == lead and r.get("person_id") and int(r.get("link_strength") or 0) >= 500}
        return LinkDecision(LinkStatus.LINKED, lead, next(iter(exact_people)) if len(exact_people) == 1 else None, "thread_inherited", 350, {"counterparties": emails, "warnings": warnings})
    operator_domain = (normalize_counterparty_email(message.account_email) or "@").rsplit("@", 1)[-1]
    candidates: set[int] = set(); domains: list[str] = []
    for email in emails:
        domain = email.rsplit("@", 1)[-1]
        if domain == operator_domain or domain in PUBLIC_EMAIL_DOMAINS: continue
        domains.append(domain); candidates.update(int(m["lead_id"]) for m in store.find_company_domain_matches(domain) if m.get("lead_id"))
    if len(candidates) > 1:
        return _ambiguous("multiple_domain_matches", emails=emails, leads=candidates, domain=domains[0] if len(domains) == 1 else None)
    if candidates:
        return LinkDecision(LinkStatus.LINKED, next(iter(candidates)), None, "unique_company_domain", 200, {"counterparties": emails, "matched_domain": domains[0] if domains else None, "warnings": warnings})
    return LinkDecision(LinkStatus.UNLINKED, reason="unmatched", strength=0, evidence={"counterparties": emails, "warnings": warnings})
