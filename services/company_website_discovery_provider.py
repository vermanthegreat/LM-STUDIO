"""Bounded, deterministic discovery from static public company-site pages."""

from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import re
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from candidate_models import normalize_candidate_name, normalize_contact_value
from discovery_models import (
    DiscoveryCandidate,
    DiscoveryContact,
    DiscoveryOutcome,
    DiscoveryOutcomeStatus,
    DiscoveryRequest,
    DiscoverySource,
    reject_secrets,
)
from providers.discovery_base import DiscoveryProvider


PROVIDER_NAME = "company_website"
SOURCE_TYPE = "website"
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_PAGE_TEXT = 20_000
MAX_JSON_LD_BYTES = 100_000
MAX_JSON_LD_ITEMS = 50
MAX_PERSON_NAME_WORDS = 6
SAFE_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_:-]{0,63}$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PHONE_RE = re.compile(r"^[+()\d][+()\d .-]{5,31}$")
PERSON_TYPES = {"person", "https://schema.org/person", "http://schema.org/person"}
PAGE_PRIORITY = MappingProxyType({
    "team": 0,
    "our-team": 0,
    "people": 0,
    "leadership": 1,
    "management": 1,
    "about": 2,
    "about-us": 2,
    "who-we-are": 2,
    "company": 3,
    "contact": 4,
})
CONVENTIONAL_PATHS = (
    "/about", "/about-us", "/team", "/our-team", "/people", "/leadership",
    "/management", "/who-we-are", "/company", "/contact",
)
ROLE_ALIASES = MappingProxyType({
    "economic_buyer": frozenset({
        "ceo", "chief executive officer", "founder", "co founder", "cofounder",
        "owner", "founder and ceo", "co founder and ceo",
    }),
    "operational_owner": frozenset({
        "head of commerce", "head of ecommerce", "head of e commerce",
        "ecommerce director", "e commerce director", "project manager",
        "ecommerce project manager", "e commerce project manager", "delivery manager",
    }),
})
IGNORED_TAGS = frozenset({"script", "style", "noscript", "svg", "template"})
BOILERPLATE_WORDS = frozenset({
    "about", "about us", "our team", "team", "people", "leadership", "management",
    "contact", "company", "careers", "services", "home", "read more", "learn more",
})


class CompanyWebsiteDiscoveryProviderError(ValueError):
    """Stable provider error without dependency or transport details."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CompanyWebsiteFetchPort(ABC):
    @abstractmethod
    def fetch(
        self,
        *,
        url: str,
        timeout_seconds: int,
        max_response_bytes: int,
    ) -> "CompanyWebsiteFetchResponse":
        raise NotImplementedError


class CompanyWebsiteFetchResponse(BaseModel):
    """Immutable bounded response returned by a fetch-port implementation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    requested_url: str = Field(min_length=1, max_length=2048)
    final_url: str = Field(min_length=1, max_length=2048)
    http_status: int = Field(ge=100, le=599)
    content_type: str = Field(min_length=1, max_length=128)
    body: bytes = Field(max_length=MAX_RESPONSE_BYTES)
    retry_after: Optional[datetime] = None
    safe_transport_code: Optional[str] = Field(default=None, max_length=64)

    @field_validator("requested_url", "final_url")
    @classmethod
    def validate_urls(cls, value: str) -> str:
        if _normalize_website_url(value, allow_query=False) is None:
            raise ValueError("fetch URL is invalid")
        return value

    @field_validator("content_type")
    @classmethod
    def normalize_content_type(cls, value: str) -> str:
        normalized = value.split(";", 1)[0].strip().casefold()
        if not normalized or len(normalized) > 128:
            raise ValueError("content type is invalid")
        return normalized

    @field_validator("safe_transport_code")
    @classmethod
    def validate_transport_code(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and (not SAFE_CODE_RE.fullmatch(value) or "secret" in value):
            raise ValueError("transport code is invalid")
        return value


class _Node:
    __slots__ = ("tag", "attrs", "children", "parent", "text")

    def __init__(self, tag: str, attrs: dict[str, str], parent: Optional["_Node"] = None) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[_Node] = []
        self.parent = parent
        self.text: list[str] = []

    def visible_text(self) -> str:
        parts: list[str] = []
        self._collect_text(parts)
        return " ".join(" ".join(parts).split())

    def _collect_text(self, parts: list[str]) -> None:
        parts.extend(item.strip() for item in self.text if item.strip())
        for child in self.children:
            child._collect_text(parts)

    def descendants(self) -> Iterable["_Node"]:
        for child in self.children:
            yield child
            yield from child.descendants()


class _StaticHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", {})
        self.current = self.root
        self.skip_depth = 0
        self.title_parts: list[str] = []
        self.json_ld: list[str] = []
        self._json_ld_depth = 0
        self._json_ld_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        normalized_attrs = {key.casefold(): value or "" for key, value in attrs}
        normalized_tag = tag.casefold()
        if normalized_tag == "script" and normalized_attrs.get("type", "").casefold() == "application/ld+json" and not self.skip_depth:
            self._json_ld_depth = 1
            return
        if normalized_tag in IGNORED_TAGS or normalized_attrs.get("aria-hidden") == "true" or "hidden" in normalized_attrs:
            self.skip_depth += 1
            return
        if self.skip_depth:
            self.skip_depth += 1
            return
        node = _Node(normalized_tag, normalized_attrs, self.current)
        self.current.children.append(node)
        self.current = node
        if normalized_tag == "script" and normalized_attrs.get("type", "").casefold() == "application/ld+json":
            self._json_ld_depth = 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        self.handle_starttag(tag, attrs)
        if self.skip_depth:
            self.skip_depth = max(0, self.skip_depth - 1)
        elif self.current is not self.root:
            self.current = self.current.parent or self.root

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag == "script" and self._json_ld_depth:
            if self._json_ld_parts:
                self.json_ld.append("".join(self._json_ld_parts))
            self._json_ld_parts = []
            self._json_ld_depth = 0
            return
        if self.skip_depth:
            self.skip_depth -= 1
            return
        node = self.current
        while node is not self.root and node.tag != normalized_tag:
            node = node.parent or self.root
        self.current = node.parent if node is not self.root and node.parent is not None else self.root

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if self._json_ld_depth:
            self._json_ld_parts.append(data)
        if self.current.tag == "title":
            self.title_parts.append(data)
        else:
            self.current.text.append(data)

    def handle_comment(self, data: str) -> None:
        return

    def parse(self, body: bytes) -> None:
        try:
            self.feed(body.decode("utf-8", errors="replace"))
            self.close()
        except Exception:
            self.root = _Node("root", {})
            self.title_parts = []
            self.json_ld = []


def _normalize_website_url(value: str, *, allow_query: bool) -> Optional[str]:
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username or parsed.password or parsed.fragment or (parsed.query and not allow_query):
            return None
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").casefold()
        if _is_blocked_host(host):
            return None
        port = parsed.port
        if port is not None and port not in {80, 443}:
            return None
        scheme = parsed.scheme.casefold()
        netloc = host
        if port is not None and port != (443 if scheme == "https" else 80):
            netloc = f"{host}:{port}"
        path = unquote(parsed.path or "/")
        if ".." in path.split("/"):
            return None
        path = re.sub(r"/{2,}", "/", path)
        if not path.startswith("/"):
            path = "/" + path
        if path != "/":
            path = path.rstrip("/") or "/"
        return urlunsplit((scheme, netloc, path, parsed.query if allow_query else "", ""))
    except (ValueError, UnicodeError):
        return None


def _is_blocked_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    return host == "localhost" or host.endswith((".local", ".internal", ".localhost")) or "." not in host


def _same_allowed_host(host: str, start_host: str) -> bool:
    root = start_host.removeprefix("www.")
    return host in {root, f"www.{root}"}


def _same_redirect_target(expected: str, actual: str) -> bool:
    expected_parts = urlsplit(expected)
    actual_parts = urlsplit(actual)
    if expected_parts.scheme != actual_parts.scheme:
        return False
    if expected_parts.path != actual_parts.path:
        return False
    return _same_allowed_host(actual_parts.hostname or "", expected_parts.hostname or "")


def _page_type(url: str, title: str = "") -> Optional[str]:
    path_parts = [part for part in urlsplit(url).path.casefold().split("/") if part]
    for part in path_parts:
        if part in PAGE_PRIORITY:
            return part
    normalized_title = re.sub(r"[^a-z0-9]+", " ", title.casefold()).strip()
    for key in PAGE_PRIORITY:
        if key.replace("-", " ") in normalized_title:
            return key
    return None


def _clean_text(value: str, limit: int = MAX_PAGE_TEXT) -> str:
    return " ".join(value.split())[:limit]


def _safe_code(value: Optional[str], fallback: str) -> str:
    return value if value and SAFE_CODE_RE.fullmatch(value) else fallback


class CompanyWebsiteDiscoveryProvider(DiscoveryProvider):
    """Inspect a finite, same-host set of static company-site pages."""

    def __init__(self, fetcher: CompanyWebsiteFetchPort) -> None:
        if not isinstance(fetcher, CompanyWebsiteFetchPort):
            raise CompanyWebsiteDiscoveryProviderError(
                "invalid_company_website_fetch_dependency", "company website fetch dependency is invalid",
            )
        self._fetcher = fetcher

    def execute(self, request: DiscoveryRequest) -> DiscoveryOutcome:
        started_at = request.requested_at
        base_url = _normalize_website_url(request.company_website, allow_query=False)
        if base_url is None or SOURCE_TYPE not in request.approved_source_types:
            return self._outcome(
                request, DiscoveryOutcomeStatus.PERMANENT_ERROR, started_at,
                safe_error_code="company_website_url_invalid" if base_url is None else "source_type_not_approved",
            )
        start_host = urlsplit(base_url).hostname or ""
        plan = self._crawl_plan(base_url, start_host, request)
        sources: list[DiscoverySource] = []
        evidence: list[tuple[DiscoverySource, _Node, Optional[str]]] = []
        warnings: list[str] = []
        candidates: list[tuple[int, int, DiscoveryCandidate]] = []
        for url in plan:
            if len(sources) >= request.max_pages or len(sources) >= 5:
                break
            try:
                response = self._fetcher.fetch(
                    url=url, timeout_seconds=request.timeout_seconds, max_response_bytes=MAX_RESPONSE_BYTES,
                )
            except Exception:
                if url == base_url:
                    return self._outcome(
                        request, DiscoveryOutcomeStatus.RETRYABLE_ERROR, started_at,
                        safe_error_code="company_website_fetch_failed",
                    )
                warnings.append("secondary_page_fetch_failed")
                continue
            if not _response_matches_request(response, url, start_host):
                if url == base_url:
                    return self._outcome(
                        request, DiscoveryOutcomeStatus.PERMANENT_ERROR, started_at,
                        safe_error_code="company_website_redirect_blocked",
                    )
                warnings.append("secondary_page_redirect_blocked")
                continue
            if response.http_status == 429:
                if url == base_url:
                    retry_after = response.retry_after or (started_at + timedelta(seconds=60))
                    return self._outcome(
                        request, DiscoveryOutcomeStatus.RATE_LIMITED, started_at,
                        retry_after=retry_after,
                    )
                warnings.append("secondary_page_rate_limited")
                continue
            if response.http_status in {401, 403}:
                if url == base_url:
                    return self._outcome(
                        request, DiscoveryOutcomeStatus.PERMANENT_ERROR, started_at,
                        safe_error_code="company_website_access_denied",
                    )
                warnings.append("secondary_page_access_denied")
                continue
            if response.http_status in {404, 410}:
                if url == base_url:
                    return self._outcome(
                        request, DiscoveryOutcomeStatus.PERMANENT_ERROR, started_at,
                        safe_error_code="company_website_not_found",
                    )
                warnings.append("secondary_page_not_found")
                continue
            if response.http_status >= 500 or response.http_status < 200 or response.http_status >= 300:
                if url == base_url:
                    return self._outcome(
                        request, DiscoveryOutcomeStatus.RETRYABLE_ERROR, started_at,
                        safe_error_code="company_website_fetch_failed",
                    )
                warnings.append("secondary_page_fetch_failed")
                continue
            if response.content_type not in {"text/html", "application/xhtml+xml"}:
                if url == base_url:
                    return self._outcome(
                        request, DiscoveryOutcomeStatus.PERMANENT_ERROR, started_at,
                        safe_error_code="company_website_unsupported_content",
                    )
                warnings.append("secondary_page_unsupported_content")
                continue
            if len(response.body) > MAX_RESPONSE_BYTES:
                if url == base_url:
                    return self._outcome(
                        request, DiscoveryOutcomeStatus.PERMANENT_ERROR, started_at,
                        safe_error_code="company_website_response_too_large",
                    )
                warnings.append("secondary_page_response_too_large")
                continue
            parser = _StaticHTMLParser()
            parser.parse(response.body)
            page_title = _clean_text(" ".join(parser.title_parts), 200)
            canonical_url = _normalize_website_url(response.final_url, allow_query=False) or url
            source = DiscoverySource(
                source_url=canonical_url,
                canonical_url=canonical_url,
                source_type=SOURCE_TYPE,
                page_title=page_title,
                retrieved_at=started_at,
                content_hash=hashlib.sha256(response.body).hexdigest(),
                extracted_text=_clean_text(parser.root.visible_text()),
                http_status=response.http_status,
                content_type=response.content_type,
                provider_request_id=request.correlation_id,
                warning_codes=[],
            )
            sources.append(source)
            page_kind = _page_type(canonical_url, page_title)
            evidence.append((source, parser.root, page_kind))
            if len(sources) == 1:
                discovered = self._eligible_links(parser.root, canonical_url, start_host, plan)
                conventional = [candidate for candidate in plan[1:] if candidate not in discovered]
                plan[1:] = (discovered + conventional)[:request.max_requests - 1]
            candidates.extend(self._extract_candidates(source, parser.root, page_kind, parser.json_ld, request))

        deduped, conflict = self._deduplicate(candidates)
        if len(deduped) > request.result_limit:
            deduped = deduped[:request.result_limit]
            warnings.append("candidate_limit_reached")
        warnings = list(dict.fromkeys(warnings))[:20]
        if conflict:
            warnings.append("candidate_conflict")
        candidate_values = [item[2] for item in deduped]
        if not sources:
            return self._outcome(
                request, DiscoveryOutcomeStatus.NO_RESULT, started_at,
                sources=[], candidates=[], warnings=warnings, no_result_reason="no_accepted_company_pages",
            )
        if conflict or (warnings and candidate_values):
            status = DiscoveryOutcomeStatus.NEEDS_REVIEW if conflict else DiscoveryOutcomeStatus.PARTIAL
        elif candidate_values:
            status = DiscoveryOutcomeStatus.SUCCEEDED
        else:
            status = DiscoveryOutcomeStatus.NO_RESULT
        return self._outcome(
            request, status, started_at, sources=sources, candidates=candidate_values,
            warnings=warnings, no_result_reason="no_matching_company_people" if status is DiscoveryOutcomeStatus.NO_RESULT else None,
        )

    def _crawl_plan(self, base_url: str, start_host: str, request: DiscoveryRequest) -> list[str]:
        return [base_url] + [
            _normalize_website_url(urlunsplit((urlsplit(base_url).scheme, start_host, path, "", "")), allow_query=False)
            for path in CONVENTIONAL_PATHS
        ][: max(0, request.max_requests - 1)]

    def _eligible_links(self, root: _Node, page_url: str, start_host: str, existing: list[str]) -> list[str]:
        found: list[tuple[int, str]] = []
        for node in root.descendants():
            if node.tag != "a" or not node.attrs.get("href"):
                continue
            candidate = _normalize_website_url(urljoin(page_url, html.unescape(node.attrs["href"])), allow_query=False)
            if candidate is None:
                continue
            parsed = urlsplit(candidate)
            if parsed.hostname is None or not _same_allowed_host(parsed.hostname, start_host):
                continue
            kind = _page_type(candidate, node.visible_text())
            if kind is not None and candidate not in existing:
                found.append((PAGE_PRIORITY[kind], candidate))
        return [url for _, url in sorted(set(found), key=lambda item: (item[0], item[1]))]

    def _extract_candidates(
        self,
        source: DiscoverySource,
        root: _Node,
        page_kind: Optional[str],
        json_ld_blocks: list[str],
        request: DiscoveryRequest,
    ) -> list[tuple[int, int, DiscoveryCandidate]]:
        extracted: list[tuple[int, int, DiscoveryCandidate]] = []
        for block in json_ld_blocks[:10]:
            if len(block.encode("utf-8")) > MAX_JSON_LD_BYTES:
                continue
            try:
                value = json.loads(block)
                items = list(_bounded_json_persons(value))[:MAX_JSON_LD_ITEMS]
            except Exception:
                continue
            for item in items:
                candidate = self._candidate_from_fields(source, item, request, method_rank=0, confidence=0.95)
                if candidate is not None:
                    extracted.append((0, 0, candidate))
        for node in root.descendants():
            if "itemscope" not in node.attrs or not _is_person_type(node.attrs.get("itemtype", "")):
                continue
            fields = _microdata_fields(node)
            candidate = self._candidate_from_fields(source, fields, request, method_rank=1, confidence=0.85)
            if candidate is not None:
                extracted.append((1, 0, candidate))
        if page_kind in {"team", "our-team", "people", "leadership", "management", "about", "about-us", "who-we-are"}:
            for node in root.descendants():
                if node.tag not in {"h2", "h3", "h4"}:
                    continue
                parent = _evidence_container(node)
                if parent is None:
                    continue
                name = node.visible_text()
                title = _local_title(parent, node)
                if not _plausible_name(name) or not title:
                    continue
                fields = {"name": name, "jobTitle": title}
                fields.update(_local_contacts(parent))
                candidate = self._candidate_from_fields(source, fields, request, method_rank=2, confidence=0.65, review=True)
                if candidate is not None:
                    extracted.append((2, 0, candidate))
        return extracted

    def _candidate_from_fields(
        self,
        source: DiscoverySource,
        fields: Mapping[str, Any],
        request: DiscoveryRequest,
        *,
        method_rank: int,
        confidence: float,
        review: bool = False,
    ) -> Optional[DiscoveryCandidate]:
        try:
            reject_secrets(fields)
        except Exception:
            return None
        name = _first_text(fields.get("name"))
        title = _first_text(fields.get("jobTitle") or fields.get("title"))
        role = _match_requested_role(title, request.target_roles)
        if not name or not title or role is None:
            return None
        try:
            normalized_name = normalize_candidate_name(name)
            contacts = _explicit_contacts(fields, source.canonical_url)
            profile = _first_text(fields.get("url"))
            if not profile:
                same_as = fields.get("sameAs") or fields.get("sameas")
                if isinstance(same_as, list) and same_as:
                    profile = _first_text(same_as[0])
                elif isinstance(same_as, str):
                    profile = same_as
            if profile and not _safe_profile(profile):
                profile = None
            signals = ["heuristic_card_requires_review"] if review else []
            candidate = DiscoveryCandidate(
                name=name[:200], normalized_name=normalized_name,
                title=title[:200], role_type=role, is_decision_maker=role == "economic_buyer",
                profile_url=profile[:2048] if profile else None, explicit_contacts=contacts,
                source_url=source.canonical_url, source_type=SOURCE_TYPE, confidence=confidence,
                relevance_reason="requested_role_match", evidence_basis="source_confirmed",
                discovery_method="deterministic_parser", raw_evidence_reference=source.canonical_url,
                ambiguity_conflict_signals=signals,
            )
            return candidate
        except Exception:
            return None

    @staticmethod
    def _deduplicate(items: list[tuple[int, int, DiscoveryCandidate]]) -> tuple[list[tuple[int, int, DiscoveryCandidate]], bool]:
        selected: dict[tuple[str, str, str], tuple[int, int, DiscoveryCandidate]] = {}
        conflicts: set[tuple[str, str]] = set()
        for item in items:
            _, order, candidate = item
            key = (candidate.normalized_name, candidate.title or "", candidate.role_type)
            name_role = (candidate.normalized_name, candidate.role_type)
            previous = selected.get(key)
            if previous is None or (item[0], -candidate.confidence, order) < (previous[0], -previous[2].confidence, previous[1]):
                selected[key] = item
            if previous and {contact.normalized_value for contact in previous[2].explicit_contacts} != {contact.normalized_value for contact in candidate.explicit_contacts}:
                conflicts.add(name_role)
        values = sorted(selected.values(), key=lambda item: (item[0], item[1], item[2].normalized_name, item[2].title or ""))
        return values, bool(conflicts)

    @staticmethod
    def _outcome(
        request: DiscoveryRequest,
        status: DiscoveryOutcomeStatus,
        started_at: datetime,
        *,
        sources: Optional[list[DiscoverySource]] = None,
        candidates: Optional[list[DiscoveryCandidate]] = None,
        warnings: Optional[list[str]] = None,
        retry_after: Optional[datetime] = None,
        no_result_reason: Optional[str] = None,
        safe_error_code: Optional[str] = None,
    ) -> DiscoveryOutcome:
        return DiscoveryOutcome(
            provider_name=PROVIDER_NAME, provider_request_id=request.correlation_id, status=status,
            started_at=started_at, completed_at=started_at, sources=sources or [], candidates=candidates or [],
            warnings=list(dict.fromkeys(warnings or []))[:20], retry_after=retry_after,
            no_result_reason=no_result_reason, safe_error_code=safe_error_code,
        )


def _response_matches_request(response: CompanyWebsiteFetchResponse, requested_url: str, start_host: str) -> bool:
    planned = _normalize_website_url(requested_url, allow_query=False)
    requested = _normalize_website_url(response.requested_url, allow_query=False)
    final = _normalize_website_url(response.final_url, allow_query=False)
    if planned is None or requested is None or final is None:
        return False
    planned_host = urlsplit(planned).hostname or ""
    if not _same_allowed_host(planned_host, start_host):
        return False
    return (
        _same_redirect_target(planned, requested)
        and _same_redirect_target(planned, final)
        and _same_redirect_target(requested, final)
    )


def _is_person_type(value: str) -> bool:
    return any(item.strip().casefold() in PERSON_TYPES for item in value.split())


def _bounded_json_persons(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        if _is_person_type(str(value.get("@type", ""))):
            yield value
        graph = value.get("@graph")
        if isinstance(graph, list):
            for item in graph[:MAX_JSON_LD_ITEMS]:
                yield from _bounded_json_persons(item)
    elif isinstance(value, list):
        for item in value[:MAX_JSON_LD_ITEMS]:
            yield from _bounded_json_persons(item)


def _microdata_fields(node: _Node) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for item in (node, *node.descendants()):
        prop = item.attrs.get("itemprop", "").casefold()
        if prop in {"name", "jobtitle", "email", "telephone", "url", "sameas"}:
            value = item.attrs.get("content") or item.attrs.get("href") or item.attrs.get("src") or item.visible_text()
            if value:
                fields[{"jobtitle": "jobTitle", "sameas": "sameAs"}.get(prop, prop)] = value
    return fields


def _first_text(value: Any) -> Optional[str]:
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, Mapping):
        value = value.get("name")
    if not isinstance(value, str):
        return None
    cleaned = " ".join(html.unescape(value).split())
    return cleaned[:200] if cleaned else None


def _explicit_contacts(fields: Mapping[str, Any], source_url: str) -> list[DiscoveryContact]:
    contacts: list[DiscoveryContact] = []
    email = _first_text(fields.get("email"))
    if email:
        email = email.removeprefix("mailto:").split("?", 1)[0].strip()
        if EMAIL_RE.fullmatch(email):
            contacts.append(DiscoveryContact(
                kind="email", value=email, normalized_value=normalize_contact_value("email", email),
                evidence_basis="source_confirmed", verification_status="unverified", source_url=source_url,
            ))
    phone = _first_text(fields.get("telephone") or fields.get("phone"))
    if phone and PHONE_RE.fullmatch(phone):
        contacts.append(DiscoveryContact(
            kind="phone", value=phone, normalized_value=normalize_contact_value("phone", phone),
            evidence_basis="source_confirmed", verification_status="unverified", source_url=source_url,
        ))
    profiles = fields.get("sameAs") or fields.get("sameas") or fields.get("profile_url")
    if isinstance(profiles, str):
        profiles = [profiles]
    if isinstance(profiles, list):
        for profile in profiles[:5]:
            profile = _first_text(profile)
            if profile and _safe_profile(profile):
                contacts.append(DiscoveryContact(
                    kind="linkedin" if "linkedin.com" in profile.casefold() else "website",
                    value=profile, normalized_value=normalize_contact_value("linkedin" if "linkedin.com" in profile.casefold() else "website", profile),
                    evidence_basis="source_confirmed", verification_status="unverified", source_url=source_url,
                ))
    return contacts[:10]


def _safe_profile(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
    except ValueError:
        return False


def _plausible_name(value: str) -> bool:
    words = value.split()
    return 1 < len(words) <= MAX_PERSON_NAME_WORDS and value.casefold() not in BOILERPLATE_WORDS and all(any(char.isalpha() for char in word) for word in words)


def _evidence_container(node: _Node) -> Optional[_Node]:
    parent = node.parent
    while parent is not None and parent.tag not in {"article", "li", "div", "section"}:
        parent = parent.parent
    return parent


def _local_title(container: _Node, heading: _Node) -> Optional[str]:
    for node in container.descendants():
        if node is heading or node.tag not in {"p", "span", "strong", "em", "div"}:
            continue
        text = node.visible_text()
        if text and len(text) <= 200 and _match_any_alias(text):
            return text
    return None


def _local_contacts(container: _Node) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for node in container.descendants():
        if node.tag != "a":
            continue
        href = node.attrs.get("href", "")
        if href.casefold().startswith("mailto:"):
            fields["email"] = href[7:]
        elif href.casefold().startswith("tel:"):
            fields["telephone"] = href[4:]
        elif _safe_profile(href):
            fields.setdefault("sameAs", []).append(href)
    return fields


def _match_any_alias(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
    return any(normalized in aliases for aliases in ROLE_ALIASES.values())


def _match_requested_role(title: Optional[str], requested_roles: Iterable[str]) -> Optional[str]:
    if not title:
        return None
    normalized = re.sub(r"[^a-z0-9]+", " ", title.casefold()).strip()
    for role in requested_roles:
        aliases = ROLE_ALIASES.get(role, frozenset({role.replace("_", " ")}))
        if normalized in aliases:
            return role
    return None
