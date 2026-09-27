from datetime import datetime, timezone

from gmail_schemas import EmailAddress, NormalizedGmailMessage
from services.gmail_linking import extract_counterparties, resolve_contact_link


class Store:
    def __init__(self, matches=None, domains=None, threads=None): self.matches, self.domains, self.threads = matches or {}, domains or {}, threads or []
    def find_exact_email_matches(self, email): return self.matches.get(email, [])
    def find_company_domain_matches(self, domain): return self.domains.get(domain, [])
    def find_thread_links(self, **_): return self.threads
    def find_exact_company_name_matches(self, _): return []


def _message(sender="Person <p@agency.test>", to=None, cc=None):
    return NormalizedGmailMessage(account_email="operator@example.com", message_id="m", thread_id="t", internal_date=datetime.now(timezone.utc), from_address=EmailAddress(email=sender), to_addresses=[EmailAddress(email=x) for x in (to or [])], cc_addresses=[EmailAddress(email=x) for x in (cc or [])])


def test_exact_person_company_domain_and_public_domain_tiers():
    person = resolve_contact_link(Store(matches={"p@agency.test": [{"kind":"person","lead_id":1,"person_id":2}]}), _message())
    assert (person.lead_id, person.person_id, person.reason, person.strength) == (1, 2, "exact_person_email", 500)
    domain = resolve_contact_link(Store(domains={"agency.test": [{"lead_id":3}]}), _message("a@agency.test"))
    assert (domain.lead_id, domain.person_id, domain.reason, domain.strength) == (3, None, "unique_company_domain", 200)
    assert resolve_contact_link(Store(domains={"gmail.com": [{"lead_id":9}]}), _message("a@gmail.com")).link_status.value == "unlinked"


def test_ambiguous_and_counterparty_extraction_are_safe_and_stable():
    ambiguous = resolve_contact_link(Store(matches={"p@agency.test": [{"kind":"person","lead_id":1,"person_id":2}, {"kind":"person","lead_id":3,"person_id":4}]}), _message())
    assert ambiguous.link_status.value == "ambiguous" and ambiguous.evidence["candidate_lead_ids"] == [1, 3]
    message = _message("operator@example.com", ["z@x.test", "operator@example.com", "a@x.test"], ["z@x.test"])
    assert extract_counterparties(message) == (["a@x.test", "z@x.test"], [])


def test_outbound_counterparties_include_to_and_cc_and_strip_aliases():
    message = _message("operator@example.com", ["z@x.test", "alias@example.com"], ["a@x.test", "z@x.test"])
    assert extract_counterparties(message, operator_aliases={"alias@example.com"}) == (["a@x.test", "z@x.test"], [])


def test_malformed_counterparty_is_ignored_with_warning():
    message = _message("not-an-address", ["good@x.test"])
    emails, warnings = extract_counterparties(message)
    assert emails == [] and warnings == ["malformed_counterparty_address"]
