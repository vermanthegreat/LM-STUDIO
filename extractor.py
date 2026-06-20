"""Parse pasted text into structured lead data with LLM + deterministic fallback."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import db
from llm import EXTRACTION_SYSTEM, extract_structured
from scoring import classify_person_title, compute_fit_score


URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
MAILTO_RE = re.compile(
    r"\[([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})\]\(mailto:[^)]+\)",
    re.I,
)
DOMAIN_RE = re.compile(
    r"(?:^|\s)((?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+"
    r"[a-zA-Z]{2,})(?:\s|$|[.,)])",
    re.I,
)
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
TIER_RATING_RE = re.compile(
    r"(Service partner|Shopify Plus Partner|Plus Partner|Premier Partner|"
    r"Select Partner|Registered Partner)\s+([\d.]+)\s*\((\d+)\)",
    re.I,
)
LINKEDIN_RE = re.compile(r"https?://(?:www\.)?linkedin\.com/[^\s<>\"']+", re.I)
DATE_RE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4})\b",
    re.I,
)
TIER_LINE_RE = re.compile(
    r"^(Service partner|Shopify Plus Partner|Plus Partner|Premier Partner|Select Partner|Registered Partner)$",
    re.I,
)
RATING_LINE_RE = re.compile(r"^\d+(?:\.\d+)?$")
REVIEW_COUNT_LINE_RE = re.compile(r"^\((\d+)\)$")


def parse_source_filter_tier(source_url: Optional[str]) -> Optional[str]:
    if not source_url:
        return None
    try:
        qs = parse_qs(urlparse(source_url).query)
        for key in ("partnerTiers", "partnertiers", "partner_tiers"):
            if key in qs and qs[key]:
                return qs[key][0]
    except Exception:
        pass
    if "partnerTiers=tier_plus" in source_url:
        return "tier_plus"
    return None


def _is_directory_listing_url(url: Optional[str]) -> bool:
    if not url:
        return False
    low = url.lower()
    return "partners.shopify.com" in low or "partnertiers=" in low


def _clean_line(line: str) -> str:
    return db.sanitize_company_name(line.strip()) or line.strip()


def _first_url(text: str) -> Optional[str]:
    m = URL_RE.search(text)
    return m.group(0).rstrip(".,)") if m else None


def _lines(text: str) -> List[str]:
    return [_clean_line(ln) for ln in text.splitlines() if ln.strip()]


def _guess_company_name(lines: List[str], source_type: str) -> Optional[str]:
    if not lines:
        return None
    if source_type == "shopify_directory":
        first = lines[0]
        if not _is_shopify_false_company_candidate(first):
            return _clean_line(first)
        return None
    skip = {"shopify", "partners", "directory", "services", "about", "home", "linkedin"}
    for ln in lines[:8]:
        low = ln.lower()
        if len(ln) < 3 or low in skip:
            continue
        if "partner" in low and len(ln) < 20:
            continue
        if re.match(r"^https?://", ln):
            continue
        if "@" in ln:
            continue
        return _clean_line(ln)
    return _clean_line(lines[0]) if lines else None


def _extract_services(text: str) -> List[str]:
    services = []
    keywords = (
        "store setup", "migration", "cro", "shopify plus", "theme",
        "custom development", "seo", "paid media", "email marketing",
        "catalog", "product management", "ongoing support", "managed services",
    )
    low = text.lower()
    for kw in keywords:
        if kw in low:
            services.append(kw.title())
    return list(dict.fromkeys(services))


def _extract_partner_tier(text: str) -> Optional[str]:
    low = text.lower()
    for tier in ("Shopify Plus Partner", "Plus Partner", "Premier Partner", "Select Partner", "Registered Partner"):
        if tier.lower() in low:
            return tier
    if "plus" in low and "partner" in low:
        return "Plus Partner"
    return None


def _is_company_website(value: str) -> bool:
    low = value.lower()
    return "shopify.com" not in low and "linkedin.com" not in low


def _normalize_website(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    v = value.strip().rstrip(".,)")
    if not v:
        return None
    if v.startswith(("http://", "https://")):
        if not _is_company_website(v):
            return None
        return db.extract_domain(v) or v
    if "@" in v or " " in v:
        return None
    if not _is_company_website(v):
        return None
    return db.extract_domain(v) or v.lower()


def _extract_email(text: str) -> Optional[str]:
    for m in MAILTO_RE.finditer(text):
        return m.group(0)
    for m in EMAIL_RE.finditer(text):
        email = m.group(0)
        if not email.lower().endswith(("shopify.com", "myshopify.com")):
            return email
    return None


def _email_value(token: str) -> str:
    m = MAILTO_RE.match(token.strip())
    if m:
        return m.group(1)
    return token.strip()


def _extract_email_candidates(text: str) -> List[str]:
    candidates: List[str] = []
    seen = set()
    for m in MAILTO_RE.finditer(text):
        token = m.group(0).strip()
        val = _email_value(token).lower()
        if val in seen:
            continue
        seen.add(val)
        candidates.append(token)
    for m in EMAIL_RE.finditer(text):
        token = m.group(0).strip()
        val = token.lower()
        if val in seen:
            continue
        seen.add(val)
        candidates.append(token)
    return candidates


def _extract_phone(text: str) -> Optional[str]:
    for m in PHONE_RE.finditer(text):
        phone = m.group(0).strip()
        if len(re.sub(r"\D", "", phone)) >= 8:
            return phone
    return None


def _section_after(text: str, header: str, stop_headers: tuple[str, ...] = ()) -> List[str]:
    """Return non-empty lines following a section header until a stop header."""
    lines = text.splitlines()
    collecting = False
    result: List[str] = []
    stop_low = {h.lower() for h in stop_headers}
    header_low = header.lower()
    for raw in lines:
        ln = raw.strip()
        low = ln.lower()
        if not collecting:
            if low == header_low or low.startswith(header_low + ":"):
                collecting = True
                if ":" in ln and ln.lower().startswith(header_low + ":"):
                    rest = ln.split(":", 1)[1].strip()
                    if rest:
                        result.append(_clean_line(rest))
            continue
        if not ln:
            if result:
                break
            continue
        if low in stop_low or any(low.startswith(s + ":") or low.startswith(s + " ") for s in stop_headers):
            break
        result.append(_clean_line(ln))
    return result


def _section_text(text: str, header: str, stop_headers: tuple[str, ...] = ()) -> Optional[str]:
    lines = _section_after(text, header, stop_headers)
    if not lines:
        return None
    return " ".join(lines).strip()[:2000] or None


def _extract_tier_signals(text: str) -> Dict[str, Any]:
    """Extract visible tier text, rating, review count, and plus partner signal."""
    plus_partner_signal = "plus partner" in text.lower()
    m = TIER_RATING_RE.search(text)
    if m:
        return {
            "partner_tier": m.group(1),
            "rating": float(m.group(2)),
            "review_count": int(m.group(3)),
            "plus_partner_signal": plus_partner_signal,
        }
    lines = _lines(text)
    for i, line in enumerate(lines[:20]):
        low = line.lower()
        if TIER_LINE_RE.match(line):
            rating = None
            review_count = None
            lookahead = lines[i + 1:i + 8]
            for la in lookahead:
                if rating is None and RATING_LINE_RE.match(la):
                    try:
                        rating = float(la)
                    except ValueError:
                        rating = None
                m_count = REVIEW_COUNT_LINE_RE.match(la)
                if m_count:
                    review_count = int(m_count.group(1))
            return {
                "partner_tier": line,
                "rating": rating,
                "review_count": review_count,
                "plus_partner_signal": plus_partner_signal,
            }
    tier = _extract_partner_tier(text)
    return {
        "partner_tier": tier,
        "rating": None,
        "review_count": None,
        "plus_partner_signal": plus_partner_signal,
    }


def _extract_website_shopify(text: str) -> Optional[str]:
    contact_lines = _section_after(
        text,
        "Contact information",
        (
            "Primary location", "Supported locations", "Languages",
            "Business description", "Industries", "Featured work",
        ),
    )
    for ln in contact_lines:
        site = _normalize_website(ln)
        if site:
            return site

    featured_start = text.lower().find("featured work")
    search_text = text if featured_start < 0 else text[:featured_start]
    for u in URL_RE.findall(search_text):
        site = _normalize_website(u)
        if site:
            return site
    for ln in _lines(search_text):
        site = _normalize_website(ln)
        if site:
            return site
    for m in DOMAIN_RE.finditer(search_text):
        site = _normalize_website(m.group(1))
        if site:
            return site
    return None


def _extract_partner_since(text: str) -> Optional[str]:
    m = re.search(r"\bPartner since\s+([A-Za-z]+\s+\d{4})\b", text, re.I)
    if m:
        return m.group(1).strip()
    return _section_text(
        text,
        "Partner since",
        ("Primary location", "Contact information", "Business description"),
    )


def _extract_email_shopify(text: str, website: Optional[str]) -> Optional[str]:
    stop_headers = (
        "Primary location", "Supported locations", "Languages",
        "Business description", "Industries", "Featured work",
    )
    contact_lines = _section_after(text, "Contact information", stop_headers)
    contact_text = "\n".join(contact_lines)
    candidates = _extract_email_candidates(contact_text) or _extract_email_candidates(text)
    if not candidates:
        return None

    domain = db.extract_domain(website) if website else None
    for token in candidates:
        email = _email_value(token).lower()
        if email.endswith(("shopify.com", "myshopify.com")):
            continue
        if domain and email.split("@")[-1] == domain:
            return token
    for token in candidates:
        email = _email_value(token).lower()
        if email.endswith(("shopify.com", "myshopify.com")):
            continue
        return token
    return None


def _extract_business_description(text: str) -> Optional[str]:
    desc = _section_text(
        text,
        "Business description",
        (
            "Plus Partner", "Other services", "More services", "Industries",
            "Featured work", "Rating", "Reviews", "What is Shopify?",
        ),
    )
    if desc:
        if TIER_RATING_RE.search(desc) and len(desc.split()) <= 6:
            return None
        return desc
    lines = _lines(text)
    for i, ln in enumerate(lines):
        if ln.lower() == "business description" and i + 1 < len(lines):
            parts = []
            for follow in lines[i + 1:]:
                low = follow.lower()
                if low in {
                    "plus partner", "other services", "more services",
                    "industries", "featured work", "rating", "reviews", "what is shopify?",
                } or "partner" in low and re.search(r"\d+\.\d+", follow):
                    break
                parts.append(follow)
            if parts:
                return " ".join(parts)[:2000]
    return None


def _extract_list_section(text: str, header: str, stop_headers: tuple[str, ...]) -> List[str]:
    items = _section_after(text, header, stop_headers)
    if items:
        return items
    lines = _lines(text)
    for i, ln in enumerate(lines):
        if ln.lower() == header.lower() and i + 1 < len(lines):
            result = []
            for follow in lines[i + 1:]:
                low = follow.lower()
                if low in {s.lower() for s in stop_headers}:
                    break
                if follow.startswith("http"):
                    break
                result.append(follow)
            return result
    return []


def _split_comma_list_items(items: List[str], stop_exact: tuple[str, ...] = ()) -> List[str]:
    """Split comma-separated section lines into individual list entries."""
    stop_low = {s.lower() for s in stop_exact}
    result: List[str] = []
    for item in items:
        for part in item.split(","):
            token = _clean_line(part)
            if not token:
                continue
            if token.lower() in stop_low:
                return result
            result.append(token)
    return result


def _extract_services_shopify(text: str) -> List[str]:
    stop_headers = (
        "More services", "Industries", "Featured work", "Rating", "Reviews",
        "Business description", "Contact information",
    )
    other_lines = _section_after(text, "Other services", stop_headers)
    if other_lines:
        return _split_comma_list_items(other_lines)

    found: List[str] = []
    lines = _lines(text)
    in_services = False
    for ln in lines:
        ln_low = ln.lower()
        if ln_low in ("plus partner", "other services", "more services"):
            in_services = True
            continue
        if ln_low in (
            "industries", "featured work", "business description",
            "contact information", "rating", "reviews",
        ):
            in_services = False
            continue
        if in_services and ln and not ln.startswith("http") and "@" not in ln and "+" not in ln[:3]:
            if ln not in found and len(ln) < 120:
                found.extend(_split_comma_list_items([ln]))
    return list(dict.fromkeys(found))


def _is_featured_work_description(line: str) -> bool:
    low = line.lower()
    if low == "view featured work":
        return True
    if "|" in line:
        return False
    if line.endswith(".") and len(line) > 50:
        return True
    words = line.split()
    if len(words) > 12 and line.endswith("."):
        return True
    return False


def _extract_featured_work(text: str) -> List[Dict[str, str]]:
    lines = _section_after(
        text,
        "Featured work",
        (
            "Rating", "Reviews", "What is Shopify?", "Shopify Editions", "Careers",
            "Investors", "Newsroom", "Sustainability", "Developer Docs", "Theme Store",
            "App Store", "Partners", "Affiliates", "Resources", "Terms of Service",
            "Legal", "Privacy Policy", "Sitemap", "Your Privacy Choices",
        ),
    )
    if not lines:
        return []
    entries: List[Dict[str, str]] = []
    current: Dict[str, str] = {}
    for ln in lines:
        if _is_featured_work_description(ln):
            if current.get("title"):
                entries.append(current)
                current = {}
            continue
        if ln.startswith("http"):
            current["url"] = ln
            if current.get("title"):
                entries.append(current)
                current = {}
            continue
        if not current.get("title"):
            current["title"] = ln
        else:
            if current.get("title"):
                entries.append(current)
            current = {"title": ln}
    if current.get("title"):
        entries.append(current)
    return entries


SHOPIFY_NOISE_EXACT = {
    "skip to content", "shopify", "log out", "help center", "browse", "service partners",
    "hire a professional", "technology solutions", "what is shopify?", "shopify editions",
    "careers", "investors", "newsroom", "sustainability", "developer docs", "theme store",
    "app store", "partners", "affiliates", "resources", "terms of service", "legal",
    "privacy policy", "sitemap", "your privacy choices",
}

SHOPIFY_FALSE_COMPANY_EXACT = {
    "store setup", "store migration", "store build or redesign", "systems integration",
    "store settings configuration", "product and collection setup", "seo", "cro",
    "paid media", "email marketing", "custom development", "theme customization",
    "united states", "canada", "india", "united kingdom", "australia", "germany",
    "france", "italy", "israel", "view all partner locations",
    "service partners", "hire a professional", "technology solutions", "browse",
    "contact", "contact information", "primary location", "supported locations",
    "languages", "business description", "industries", "featured work", "rating",
    "reviews", "about", "plus partner", "other services", "more services",
}


def _is_shopify_noise_line(line: str) -> bool:
    low = line.strip().lower()
    if not low:
        return True
    if low in SHOPIFY_NOISE_EXACT:
        return True
    if low.startswith("search by keyword"):
        return True
    if low.startswith("price range for selected services"):
        return True
    if "shopify partner directory" in low:
        return True
    if EMAIL_RE.fullmatch(low) and low.endswith("gmail.com"):
        return True
    return False


def _is_shopify_false_company_candidate(line: str) -> bool:
    low = line.strip().lower()
    if not low or _is_shopify_noise_line(line):
        return True
    if low in SHOPIFY_FALSE_COMPANY_EXACT:
        return True
    if TIER_LINE_RE.match(line):
        return True
    if low.startswith("partner since"):
        return True
    if RATING_LINE_RE.match(line) or REVIEW_COUNT_LINE_RE.match(line):
        return True
    if "@" in line or low.startswith("http"):
        return True
    if len(line) < 2 or len(line) > 80:
        return True
    return False


def _following_nonempty_lines(lines: List[str], start: int, limit: int = 8) -> List[str]:
    following: List[str] = []
    for ln in lines[start + 1:]:
        if not ln.strip():
            continue
        following.append(ln)
        if len(following) >= limit:
            break
    return following


def _count_shopify_profile_signals(following: List[str]) -> int:
    signals: set[str] = set()
    for i, ln in enumerate(following):
        low = ln.lower()
        if TIER_LINE_RE.match(ln):
            signals.add("tier")
        if low.startswith("partner since"):
            signals.add("partner_since")
        if low in ("contact", "contact information") or low.startswith("contact information"):
            signals.add("contact")
        if low.startswith("price range for selected services"):
            signals.add("price_range")
        if RATING_LINE_RE.match(ln):
            nxt = following[i + 1] if i + 1 < len(following) else ""
            nxt2 = following[i + 2] if i + 2 < len(following) else ""
            if REVIEW_COUNT_LINE_RE.match(nxt) or (
                nxt == "|" and REVIEW_COUNT_LINE_RE.match(nxt2)
            ):
                signals.add("rating")
    return len(signals)


def _find_shopify_profile_start(lines: List[str]) -> int:
    for i, ln in enumerate(lines):
        if _is_shopify_false_company_candidate(ln):
            continue
        following = _following_nonempty_lines(lines, i, limit=8)
        if _count_shopify_profile_signals(following) >= 2:
            return i
    return 0


def _prepare_shopify_profile_text(text: str) -> str:
    lines = _lines(text)
    if not lines:
        return text
    start = _find_shopify_profile_start(lines)
    tail = lines[start:]
    cut_idx = len(tail)
    for i, ln in enumerate(tail):
        low = ln.lower()
        if low in {
            "what is shopify?", "shopify editions", "careers", "investors", "newsroom",
            "sustainability", "developer docs", "theme store", "app store", "partners",
            "affiliates", "resources", "terms of service", "legal", "privacy policy",
            "sitemap", "your privacy choices",
        }:
            cut_idx = i
            break
    profile_lines = [ln for ln in tail[:cut_idx] if not _is_shopify_noise_line(ln)]
    return "\n".join(profile_lines) if profile_lines else "\n".join(tail[:cut_idx])


def _parse_shopify_directory(text: str) -> Dict[str, Any]:
    profile_text = _prepare_shopify_profile_text(text)
    lines = _lines(profile_text)
    company = _guess_company_name(lines, "shopify_directory")
    tier_info = _extract_tier_signals(profile_text)
    description = _extract_business_description(profile_text)
    if not description and len(lines) > 1:
        fallback = " ".join(lines[1:4])[:500]
        if not TIER_RATING_RE.search(fallback) and "partner" not in fallback.lower()[:30]:
            description = fallback

    services = _extract_services_shopify(profile_text)
    industries = _split_comma_list_items(
        _extract_list_section(
            profile_text, "Industries", ("Featured work", "Business description"),
        ),
    )
    supported = _split_comma_list_items(
        _extract_list_section(
            profile_text,
            "Supported locations",
            ("Languages", "Business description", "Industries"),
        ),
    )
    languages = _split_comma_list_items(
        _extract_list_section(
            profile_text,
            "Languages",
            ("About", "Business description", "Industries", "Featured work"),
        ),
        stop_exact=("About",),
    )
    primary = _section_text(
        profile_text, "Primary location", ("Supported locations", "Languages", "Business description"),
    )
    partner_since = _extract_partner_since(profile_text)
    website = _extract_website_shopify(profile_text)

    return {
        "company_name": company,
        "website": website,
        "company_email": _extract_email_shopify(profile_text, website),
        "company_phone": _extract_phone(profile_text),
        "partner_tier": tier_info["partner_tier"],
        "plus_partner_signal": tier_info["plus_partner_signal"],
        "rating": tier_info["rating"],
        "review_count": tier_info["review_count"],
        "partner_since": partner_since,
        "primary_location": primary,
        "supported_locations": supported,
        "languages": languages,
        "services": services,
        "locations": supported,
        "industries": industries,
        "description": description,
        "featured_work": _extract_featured_work(profile_text),
        "people": [],
        "interaction": None,
        "confidence": 0.5,
        "raw_text": text,
    }


def _parse_linkedin_company(text: str) -> Dict[str, Any]:
    lines = _lines(text)
    company = _guess_company_name(lines, "linkedin_company")
    li = _first_url(text)
    website = None
    for u in URL_RE.findall(text):
        if "linkedin.com" not in u.lower():
            website = u.rstrip(".,)")
            break
    return {
        "company_name": company,
        "website": website,
        "partner_tier": _extract_partner_tier(text),
        "services": _extract_services(text),
        "locations": [],
        "industries": [],
        "description": " ".join(lines[1:6])[:500] if len(lines) > 1 else None,
        "people": [],
        "interaction": None,
        "confidence": 0.45,
    }


def _parse_linkedin_person(text: str) -> Dict[str, Any]:
    lines = _lines(text)
    name = lines[0] if lines else None
    title = lines[1] if len(lines) > 1 else None
    li = None
    for m in LINKEDIN_RE.findall(text):
        if "/in/" in m or "/pub/" in m:
            li = m.rstrip(".,)")
            break
    company = None
    for ln in lines[2:8]:
        if " at " in ln.lower():
            company = ln.split(" at ")[-1].strip()
            break
        if ln.lower().startswith("experience") or ln.lower().startswith("company"):
            continue
    if not company and len(lines) > 2:
        company = lines[2]
    person = {"name": name, "title": title, "linkedin_url": li, "department": None}
    return {
        "company_name": company,
        "website": None,
        "partner_tier": None,
        "services": [],
        "locations": [],
        "industries": [],
        "description": None,
        "people": [person] if name else [],
        "interaction": None,
        "confidence": 0.5,
    }


def _parse_website(text: str) -> Dict[str, Any]:
    lines = _lines(text)
    website = _first_url(text)
    company = _guess_company_name(lines, "website")
    if not company and website:
        company = db.extract_domain(website)
    return {
        "company_name": company,
        "website": website,
        "partner_tier": _extract_partner_tier(text),
        "services": _extract_services(text),
        "locations": [],
        "industries": [],
        "description": " ".join(lines[:5])[:500],
        "people": [],
        "interaction": None,
        "confidence": 0.4,
    }


def _parse_header_contacts(text: str) -> List[Dict[str, Any]]:
    """Extract name/email pairs from Gmail-style From/To/Cc lines."""
    people: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for line in text.splitlines()[:40]:
        stripped = line.strip()
        low = stripped.lower()
        if not any(low.startswith(prefix) for prefix in ("from:", "to:", "cc:", "reply-to:")):
            continue
        rest = stripped.split(":", 1)[1].strip()
        angle = re.search(r"<([^>]+@[^>]+)>", rest)
        if angle:
            email = _email_value(angle.group(1))
            name = rest[: angle.start()].strip().strip('"').strip("'")
        else:
            em = EMAIL_RE.search(rest)
            if not em:
                continue
            email = em.group(0)
            name = rest.replace(email, "").strip().strip('"').strip("'")
        norm = db.normalize_email(email)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        people.append({"name": name or None, "email": email})
    return people


def _pick_company_email(candidates: List[str]) -> Optional[str]:
    for token in candidates:
        email = _email_value(token)
        if db.is_business_email(email):
            return email
    return None


def _parse_email(text: str) -> Dict[str, Any]:
    lines = _lines(text)
    subject = None
    body_start = 0
    for i, ln in enumerate(lines[:10]):
        if ln.lower().startswith("subject:"):
            subject = ln.split(":", 1)[1].strip()
            body_start = i + 1
            break
    if not subject and lines:
        subject = lines[0]
        body_start = 1
    body = "\n".join(lines[body_start:])
    reply_needed = any(
        w in text.lower()
        for w in ("?", "please reply", "let me know", "follow up", "waiting for", "deadline", "asap")
    )
    deadline = None
    dm = DATE_RE.search(text)
    if dm:
        deadline = dm.group(1)

    people = _parse_header_contacts(text)
    candidates = _extract_email_candidates(text)
    company_email = _pick_company_email(candidates)

    company = None
    for token in candidates:
        email = _email_value(token)
        if db.is_business_email(email):
            domain = db.email_domain(email)
            company = domain.split(".")[0].title() if domain else None
            break

    return {
        "company_name": company,
        "company_email": company_email,
        "website": None,
        "partner_tier": None,
        "services": [],
        "locations": [],
        "industries": [],
        "description": None,
        "people": people,
        "interaction": {
            "subject": subject,
            "summary": body[:300] if body else None,
            "reply_needed": reply_needed,
            "deadline": deadline,
            "next_action": "Reply to email" if reply_needed else None,
        },
        "confidence": 0.55,
    }


def _parse_note(text: str) -> Dict[str, Any]:
    return {
        "company_name": None,
        "website": _first_url(text),
        "partner_tier": None,
        "services": _extract_services(text),
        "locations": [],
        "industries": [],
        "description": text[:1000],
        "people": [],
        "interaction": None,
        "confidence": 0.3,
    }


FALLBACK_PARSERS = {
    "shopify_directory": _parse_shopify_directory,
    "linkedin_company": _parse_linkedin_company,
    "linkedin_person": _parse_linkedin_person,
    "website": _parse_website,
    "email": _parse_email,
    "note": _parse_note,
}


def deterministic_parse(source_type: str, raw_text: str) -> Dict[str, Any]:
    parser = FALLBACK_PARSERS.get(source_type, _parse_note)
    return parser(raw_text)


def _merge_email_fields(parsed: Dict[str, Any], raw_text: str) -> None:
    """Keep Gmail/Outlook address fields when LLM extraction drops them."""
    email_parsed = _parse_email(raw_text)
    if email_parsed.get("company_email") and not parsed.get("company_email"):
        parsed["company_email"] = email_parsed["company_email"]
    if not parsed.get("interaction") and email_parsed.get("interaction"):
        parsed["interaction"] = email_parsed["interaction"]

    email_people = email_parsed.get("people") or []
    by_email = {
        db.normalize_email(p["email"]): p
        for p in email_people
        if p.get("email")
    }
    people = list(parsed.get("people") or [])
    for person in people:
        if person.get("email"):
            continue
        for ep in email_people:
            if not ep.get("email"):
                continue
            if person.get("name") and ep.get("name") and person["name"].lower() == ep["name"].lower():
                person["email"] = ep["email"]
                break
    known = {db.normalize_email(p.get("email")) for p in people if p.get("email")}
    for ep in email_people:
        em = db.normalize_email(ep.get("email"))
        if em and em not in known:
            people.append(ep)
            known.add(em)
    parsed["people"] = people


def parse_and_save(
    source_type: str,
    raw_text: str,
    source_url: Optional[str] = None,
    attach_to_lead_id: Optional[int] = None,
    db_path=None,
    store=None,
) -> Dict[str, Any]:
    """Main entry: parse pasted text, persist everything, return summary."""
    if store is None:
        from repositories.sqlite_store import SqliteContactStore

        store = SqliteContactStore(db_path or db.DB_PATH)

    parsed, llm_raw = extract_structured(
        EXTRACTION_SYSTEM,
        f"source_type={source_type}\nsource_url={source_url or ''}\n\nPASTED TEXT:\n{raw_text}",
    )
    extraction_status = "ok"
    confidence = 0.0

    if parsed and isinstance(parsed, dict):
        confidence = float(parsed.get("confidence") or 0.7)
    else:
        parsed = deterministic_parse(source_type, raw_text)
        extraction_status = "fallback"
        confidence = float(parsed.get("confidence") or 0.4)
        if llm_raw:
            extraction_status = "needs_review"

    if "raw_text" not in parsed:
        parsed["raw_text"] = raw_text

    if source_type == "email":
        _merge_email_fields(parsed, raw_text)

    source_filter_tier = parse_source_filter_tier(source_url)

    with store.transaction():
        lead_id = attach_to_lead_id
        if not lead_id and parsed.get("company_name"):
            matches = store.find_matching_leads(
                parsed.get("company_name"),
                parsed.get("website"),
            )
            if matches:
                lead_id = matches[0]["id"]

        if not lead_id and source_type == "email":
            for token in _extract_email_candidates(raw_text):
                matches = store.find_leads_by_email(_email_value(token))
                if matches:
                    lead_id = matches[0]["id"]
                    break

        website_fallback = None
        if source_url and not _is_directory_listing_url(source_url):
            website_fallback = source_url

        lead = None
        if lead_id or parsed.get("company_name") or source_type in (
            "shopify_directory", "linkedin_company", "website", "note"
        ):
            lead_data = {
                "company_name": store.sanitize_company_name(parsed.get("company_name"))
                or (f"Unknown ({source_type})" if not lead_id else None),
                "website": parsed.get("website") or website_fallback,
                "company_email": parsed.get("company_email"),
                "company_phone": parsed.get("company_phone"),
                "partner_tier": parsed.get("partner_tier"),
                "plus_partner_signal": parsed.get("plus_partner_signal"),
                "rating": parsed.get("rating"),
                "review_count": parsed.get("review_count"),
                "partner_since": parsed.get("partner_since"),
                "primary_location": parsed.get("primary_location"),
                "supported_locations": parsed.get("supported_locations") or [],
                "languages": parsed.get("languages") or [],
                "featured_work": parsed.get("featured_work") or [],
                "services": parsed.get("services") or [],
                "locations": parsed.get("locations") or parsed.get("supported_locations") or [],
                "industries": parsed.get("industries") or [],
                "description": parsed.get("description"),
                "confidence": confidence,
                "extraction_status": extraction_status,
            }
            if lead_id:
                if source_type == "email":
                    lead_data.pop("company_name", None)
                lead, _ = store.upsert_lead(lead_data, lead_id=lead_id)
            elif lead_data.get("company_name"):
                lead, _ = store.upsert_lead(lead_data)
                lead_id = lead["id"]

        raw_source = store.create_raw_source(
            source_type=source_type,
            raw_text=raw_text,
            source_url=source_url,
            source_filter_tier=source_filter_tier,
            parsed_json=parsed,
            extraction_status=extraction_status,
            confidence=confidence,
            lead_id=lead_id,
        )

        if lead_id and raw_source.get("lead_id") != lead_id:
            store.link_raw_source_to_lead(raw_source["id"], lead_id)
            raw_source["lead_id"] = lead_id

        people_saved = []
        for person in parsed.get("people") or []:
            if not person.get("name") and not person.get("email"):
                continue
            if not lead_id:
                continue
            cls = classify_person_title(person.get("title"))
            p = store.add_person(
                lead_id,
                {
                    **person,
                    **cls,
                    "confidence": confidence,
                },
                raw_source_id=raw_source["id"],
            )
            people_saved.append(p)

        interaction_saved = None
        task_saved = None
        interaction = parsed.get("interaction")
        if interaction and lead_id:
            interaction_saved = store.add_interaction(
                lead_id,
                {
                    "type": "email" if source_type == "email" else "note",
                    "subject": interaction.get("subject"),
                    "body": raw_text if source_type == "email" else None,
                    "summary": interaction.get("summary"),
                    "reply_needed": interaction.get("reply_needed", False),
                    "deadline": interaction.get("deadline"),
                    "priority": "high" if interaction.get("reply_needed") else "normal",
                    "next_action": interaction.get("next_action"),
                },
                raw_source_id=raw_source["id"],
            )
            if interaction.get("reply_needed") or interaction.get("deadline"):
                task_saved = store.add_task(
                    lead_id,
                    {
                        "title": interaction.get("next_action") or interaction.get("subject") or "Follow up",
                        "due_date": interaction.get("deadline"),
                        "priority": "high" if interaction.get("reply_needed") else "normal",
                        "source_interaction_id": interaction_saved["id"],
                    },
                )

        if lead_id:
            lead_row = store.get_lead(lead_id)
            if lead_row:
                fit = compute_fit_score(
                    {
                        "company_name": lead_row.get("company_name"),
                        "website": lead_row.get("website"),
                        "domain": lead_row.get("domain"),
                        "partner_tier": lead_row.get("partner_tier"),
                        "services": lead_row.get("services"),
                        "industries": lead_row.get("industries"),
                        "description": lead_row.get("description"),
                    },
                    lead_row.get("people"),
                )
                store.update_lead_fit_score(lead_id, fit)
                lead_row["fit_score"] = fit

    return {
        "extraction_status": extraction_status,
        "confidence": confidence,
        "raw_source_id": raw_source["id"],
        "lead_id": lead_id,
        "people_count": len(people_saved),
        "interaction_id": interaction_saved["id"] if interaction_saved else None,
        "task_id": task_saved["id"] if task_saved else None,
        "parsed": parsed,
    }
