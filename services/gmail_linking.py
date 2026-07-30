"""Safe deterministic contact linking for imported Gmail messages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable

import db
from gmail_schemas import EmailDirection, LinkStatus, NormalizedGmailMessage
from services.email_classification_service import determine_direction


@runtime_checkable
class GmailLinkStore(Protocol):
    def find_exact_email_matches(self, email: str) -> list[dict[str, Any]]: ...

    def find_thread_links(
        self,
        *,
        external_account: str,
        external_thread_id: str,
        exclude_message_id: str,
    ) -> list[dict[str, Any]]: ...

    def find_exact_company_name_matches(self, company_name: str) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class LinkDecision:
    link_status: LinkStatus
    lead_id: Optional[int] = None
    person_id: Optional[int] = None
    review_hint: Optional[str] = None


def _normalized_sender_email(message: NormalizedGmailMessage, direction: EmailDirection) -> str:
    if direction == EmailDirection.OUTBOUND:
        for addr in message.to_addresses:
            if addr.email and addr.email.lower() != message.account_email.lower():
                return addr.email.lower()
    return (message.from_address.email or "").lower()


def _person_matches(store: GmailLinkStore, email: str) -> list[dict[str, Any]]:
    if not email:
        return []
    return [m for m in store.find_exact_email_matches(email) if m.get("kind") == "person"]


def _organization_matches(store: GmailLinkStore, email: str) -> list[dict[str, Any]]:
    if not email:
        return []
    return [m for m in store.find_exact_email_matches(email) if m.get("kind") == "organization"]


def resolve_contact_link(
    store: GmailLinkStore,
    message: NormalizedGmailMessage,
) -> LinkDecision:
    from services.email_classification_service import (
        extract_shopify_partner_inquiry_target,
        is_shopify_partner_inquiry_confirmation,
    )

    direction = determine_direction(message)
    if is_shopify_partner_inquiry_confirmation(message):
        target_company_name = extract_shopify_partner_inquiry_target(message) or ""
        company_matches = store.find_exact_company_name_matches(target_company_name)
        unique_company_ids = {m["lead_id"] for m in company_matches if m.get("lead_id")}
        if len(unique_company_ids) > 1:
            return LinkDecision(
                link_status=LinkStatus.AMBIGUOUS,
                review_hint=f"Multiple exact company-name matches for {target_company_name}",
            )
        if len(unique_company_ids) == 1:
            return LinkDecision(link_status=LinkStatus.LINKED, lead_id=int(next(iter(unique_company_ids))))

        thread_links = store.find_thread_links(
            external_account=message.account_email,
            external_thread_id=message.thread_id,
            exclude_message_id=message.message_id,
        )
        thread_pairs = {(row.get("lead_id"), row.get("person_id")) for row in thread_links if row.get("lead_id")}
        if len(thread_pairs) > 1:
            return LinkDecision(
                link_status=LinkStatus.AMBIGUOUS,
                review_hint="Conflicting thread linkage evidence",
            )
        if len(thread_pairs) == 1:
            lead_id, person_id = next(iter(thread_pairs))
            return LinkDecision(
                link_status=LinkStatus.LINKED,
                lead_id=int(lead_id) if lead_id is not None else None,
                person_id=int(person_id) if person_id is not None else None,
            )
        return LinkDecision(link_status=LinkStatus.UNLINKED)

    sender_email = _normalized_sender_email(message, direction)
    if not sender_email:
        return LinkDecision(link_status=LinkStatus.UNLINKED)
    if sender_email.endswith("@shopify.com"):
        return LinkDecision(
            link_status=LinkStatus.UNLINKED,
            review_hint="Shopify sender is provider transport evidence; no sender auto-link",
        )

    person_matches = _person_matches(store, sender_email)
    org_matches = _organization_matches(store, sender_email)
    exact_matches = person_matches + org_matches

    unique_pairs = {(m["lead_id"], m.get("person_id")) for m in exact_matches}
    if len(unique_pairs) > 1:
        return LinkDecision(
            link_status=LinkStatus.AMBIGUOUS,
            review_hint=f"Multiple exact matches for {sender_email}",
        )
    if len(unique_pairs) == 1:
        match = exact_matches[0]
        return LinkDecision(
            link_status=LinkStatus.LINKED,
            lead_id=int(match["lead_id"]),
            person_id=int(match["person_id"]) if match.get("person_id") else None,
        )

    thread_links = store.find_thread_links(
        external_account=message.account_email,
        external_thread_id=message.thread_id,
        exclude_message_id=message.message_id,
    )
    thread_pairs = {(row.get("lead_id"), row.get("person_id")) for row in thread_links if row.get("lead_id")}
    if len(thread_pairs) > 1:
        return LinkDecision(
            link_status=LinkStatus.AMBIGUOUS,
            review_hint="Conflicting thread linkage evidence",
        )
    if len(thread_pairs) == 1:
        lead_id, person_id = next(iter(thread_pairs))
        return LinkDecision(
            link_status=LinkStatus.LINKED,
            lead_id=int(lead_id) if lead_id is not None else None,
            person_id=int(person_id) if person_id is not None else None,
        )

    domain = db.email_domain(sender_email)
    if domain and not db.is_business_email(sender_email):
        return LinkDecision(
            link_status=LinkStatus.UNLINKED,
            review_hint=f"Personal-domain sender {domain}; review only",
        )
    if domain:
        return LinkDecision(
            link_status=LinkStatus.UNLINKED,
            review_hint=f"Domain-only evidence ({domain}); no auto-link",
        )
    return LinkDecision(link_status=LinkStatus.UNLINKED)
