#!/usr/bin/env python3
"""
CommerceGov local lead ops assistant.

Purpose:
- Ingest raw .txt notes, company data, emails, and browser research dumps.
- Ask a local LM Studio model to extract structured lead intelligence.
- Maintain one text-first source of truth:
  - data/companies/<company_slug>.txt
  - data/conversations/<company_slug>.txt
  - data/contacts_index.json
  - data/emails.jsonl
  - data/outbox/*.txt

No cloud API is required. Uses the LM Studio OpenAI-compatible local endpoint.
Default endpoint: http://localhost:1234/v1
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
INBOX = DATA / "inbox"
COMPANIES = DATA / "companies"
CONVERSATIONS = DATA / "conversations"
OUTBOX = DATA / "outbox"

CONTACTS_INDEX = DATA / "contacts_index.json"
EMAILS_LOG = DATA / "emails.jsonl"

LMSTUDIO_BASE_URL = os.getenv("LMSTUDIO_BASE_URL", "http://localhost:1234/v1").rstrip("/")
LMSTUDIO_MODEL = os.getenv("LMSTUDIO_MODEL", "").strip()


class LeadOpsError(RuntimeError):
    pass


def ensure_dirs() -> None:
    for path in [DATA, INBOX, COMPANIES, CONVERSATIONS, OUTBOX]:
        path.mkdir(parents=True, exist_ok=True)
    if not CONTACTS_INDEX.exists():
        CONTACTS_INDEX.write_text("{}", encoding="utf-8")
    if not EMAILS_LOG.exists():
        EMAILS_LOG.write_text("", encoding="utf-8")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "unknown"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def load_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def post_json(url: str, payload: dict[str, Any], timeout: int = 180) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise LeadOpsError(
            f"Cannot reach LM Studio at {url}. Start LM Studio local server first. Error: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise LeadOpsError(f"LM Studio returned non-JSON response from {url}") from exc


def get_json(url: str, timeout: int = 20) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return {}


def resolve_model() -> str:
    if LMSTUDIO_MODEL:
        return LMSTUDIO_MODEL

    models = get_json(f"{LMSTUDIO_BASE_URL}/models")
    data = models.get("data") or []
    if isinstance(data, list):
        for item in data:
            model_id = item.get("id") if isinstance(item, dict) else None
            if model_id:
                return str(model_id)

    return "local-model"


def extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()

    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        obj = json.loads(text[start : end + 1])
        if isinstance(obj, dict):
            return obj

    raise LeadOpsError("Model did not return a valid JSON object.")


def llm_json(system_prompt: str, user_prompt: str) -> dict[str, Any]:
    model = resolve_model()
    payload = {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    output = post_json(f"{LMSTUDIO_BASE_URL}/chat/completions", payload)
    try:
        content = output["choices"][0]["message"]["content"]
    except Exception as exc:
        raise LeadOpsError(f"Unexpected LM Studio response shape: {output}") from exc
    return extract_json_object(content)


INGEST_SYSTEM = """
You are a strict lead-intelligence extraction engine for CommerceGov.

CommerceGov context:
- Governance control plane for AI-assisted Shopify product mutations.
- Relevant buyers: Shopify agencies, ecommerce operators, catalog teams, AI commerce teams.
- Strong signals: many Shopify stores, many SKUs, SEO/content operations, AI content generation,
  approval workflow pain, compliance/governance concern, agency managing multiple clients,
  product data quality problems, Shopify Plus, PIM/catalog complexity.

Return ONLY valid JSON. No markdown. No commentary. Do not invent facts.
""".strip()

INGEST_SCHEMA_PROMPT = """
Extract structured lead intelligence from the raw text.

Return this exact JSON shape:

{
  "companies": [
    {
      "name": "",
      "website": "",
      "country": "",
      "industry": "",
      "fit_score": 0,
      "fit_reason": "",
      "commercegov_signals": [],
      "risks_or_objections": [],
      "recommended_next_action": "",
      "priority": "low|medium|high"
    }
  ],
  "contacts": [
    {
      "name": "",
      "email": "",
      "role": "",
      "company": "",
      "linkedin": "",
      "notes": ""
    }
  ],
  "emails": [
    {
      "company": "",
      "contact_email": "",
      "direction": "inbound|outbound|unknown",
      "status": "draft|sent|replied|bounced|unknown",
      "subject": "",
      "date": "",
      "summary": "",
      "next_action": ""
    }
  ],
  "conversation_notes": [
    {
      "company": "",
      "summary": "",
      "open_questions": [],
      "promises_made": [],
      "next_action": ""
    }
  ]
}

Rules:
- If unknown, use empty string or empty list.
- fit_score is 0-100.
- Prefer exact company/contact names from source.
- Keep summaries compact.
""".strip()

DRAFT_SYSTEM = """
You write concise B2B outreach emails for CommerceGov.

Rules:
- No hype.
- No fake claims.
- No invented case studies.
- Assume cold or warm B2B outreach.
- Output ONLY valid JSON:
{
  "subject": "",
  "body": ""
}
""".strip()


def render_company_file(company: dict[str, Any], existing: str | None = None) -> str:
    name = company.get("name") or "Unknown"
    updated = now_iso()
    signals = "\n".join(f"- {item}" for item in company.get("commercegov_signals", []) if item)
    risks = "\n".join(f"- {item}" for item in company.get("risks_or_objections", []) if item)

    previous = ""
    if existing:
        previous = "\n\n## Previous record snapshot\n\n" + existing.strip()[:6000]

    return f"""# {name}

Updated: {updated}

## Company

- Website: {company.get('website', '')}
- Country: {company.get('country', '')}
- Industry: {company.get('industry', '')}
- Priority: {company.get('priority', '')}
- Fit score: {company.get('fit_score', 0)}/100

## Fit reason

{company.get('fit_reason', '')}

## CommerceGov signals

{signals or '- '}

## Risks / objections

{risks or '- '}

## Recommended next action

{company.get('recommended_next_action', '')}
{previous}
"""


def merge_contacts(new_contacts: list[dict[str, Any]]) -> None:
    index = load_json(CONTACTS_INDEX, {})
    if not isinstance(index, dict):
        index = {}

    for contact in new_contacts:
        email = (contact.get("email") or "").strip().lower()
        name = (contact.get("name") or "").strip()
        company = (contact.get("company") or "").strip()
        key = email or slugify(f"{name}-{company}")
        if not key or key == "unknown":
            continue

        old = index.get(key, {})
        merged = {**old, **{k: v for k, v in contact.items() if v not in ("", None, [])}}
        merged["updated_at"] = now_iso()
        index[key] = merged

    write_text(CONTACTS_INDEX, json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True))


def append_emails(emails: list[dict[str, Any]], source_file: str) -> None:
    for email in emails:
        append_jsonl(EMAILS_LOG, {"logged_at": now_iso(), "source_file": source_file, **email})


def append_conversation_notes(notes: list[dict[str, Any]], source_file: str) -> None:
    for note in notes:
        company = note.get("company") or "unknown"
        slug = slugify(company)
        path = CONVERSATIONS / f"{slug}.txt"
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        open_questions = "\n".join(f"- {item}" for item in note.get("open_questions", []) if item)
        promises = "\n".join(f"- {item}" for item in note.get("promises_made", []) if item)

        block = f"""
## Entry — {now_iso()}

Source: {source_file}

Summary:
{note.get('summary', '')}

Open questions:
{open_questions or '- '}

Promises made:
{promises or '- '}

Next action:
{note.get('next_action', '')}

---
""".strip()
        write_text(path, (existing.strip() + "\n\n" + block).strip() + "\n")


def ingest_file(path: Path) -> None:
    ensure_dirs()
    if not path.exists():
        raise SystemExit(f"File not found: {path}")

    raw = read_text(path)
    if not raw.strip():
        raise SystemExit(f"Empty file: {path}")

    prompt = INGEST_SCHEMA_PROMPT + "\n\nRAW TEXT:\n" + raw[:50000]
    data = llm_json(INGEST_SYSTEM, prompt)

    companies = data.get("companies") or []
    contacts = data.get("contacts") or []
    emails = data.get("emails") or []
    notes = data.get("conversation_notes") or []

    if not isinstance(companies, list):
        companies = []
    if not isinstance(contacts, list):
        contacts = []
    if not isinstance(emails, list):
        emails = []
    if not isinstance(notes, list):
        notes = []

    for company in companies:
        if not isinstance(company, dict):
            continue
        name = company.get("name") or "unknown"
        out_path = COMPANIES / f"{slugify(name)}.txt"
        existing = read_text(out_path) if out_path.exists() else None
        write_text(out_path, render_company_file(company, existing=existing))

    merge_contacts([c for c in contacts if isinstance(c, dict)])
    append_emails([e for e in emails if isinstance(e, dict)], source_file=str(path))
    append_conversation_notes([n for n in notes if isinstance(n, dict)], source_file=str(path))

    print(f"ingested={path}")
    print(f"companies={len(companies)} contacts={len(contacts)} emails={len(emails)} notes={len(notes)}")


def load_company_context(company_query: str) -> tuple[str, str]:
    slug = slugify(company_query)
    direct = COMPANIES / f"{slug}.txt"
    if direct.exists():
        return slug, read_text(direct)

    for candidate in sorted(COMPANIES.glob("*.txt")):
        if slug in candidate.stem or candidate.stem in slug:
            return candidate.stem, read_text(candidate)

    raise SystemExit(f"No company file found for: {company_query}")


def draft_email(company: str, goal: str) -> None:
    ensure_dirs()
    slug, context = load_company_context(company)
    contacts = load_json(CONTACTS_INDEX, {})
    if not isinstance(contacts, dict):
        contacts = {}

    relevant_contacts = [
        value
        for value in contacts.values()
        if isinstance(value, dict)
        and (slugify(value.get("company", "")) == slug or slug in slugify(value.get("company", "")))
    ]

    prompt = f"""
Goal:
{goal}

Company context:
{context[:12000]}

Known contacts:
{json.dumps(relevant_contacts, ensure_ascii=False, indent=2)[:8000]}

Write a short outreach email.
""".strip()

    draft = llm_json(DRAFT_SYSTEM, prompt)
    subject = str(draft.get("subject", "")).strip()
    body = str(draft.get("body", "")).strip()

    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = OUTBOX / f"{timestamp}-{slug}.txt"
    write_text(out_path, f"Subject: {subject}\n\n{body}\n")

    append_jsonl(
        EMAILS_LOG,
        {
            "logged_at": now_iso(),
            "company": company,
            "direction": "outbound",
            "status": "draft",
            "subject": subject,
            "summary": goal,
            "outbox_file": str(out_path),
        },
    )

    print(f"draft_created={out_path}")


def list_companies() -> None:
    ensure_dirs()
    files = sorted(COMPANIES.glob("*.txt"))
    if not files:
        print("No companies yet.")
        return

    for file in files:
        text = read_text(file)
        title = text.splitlines()[0].replace("#", "").strip() if text else file.stem
        fit = re.search(r"Fit score:\s*(\d+)", text)
        score = fit.group(1) if fit else "?"
        print(f"{file.stem:35} score={score:>3}  {title}")


def show_contacts() -> None:
    ensure_dirs()
    contacts = load_json(CONTACTS_INDEX, {})
    if not isinstance(contacts, dict):
        print("No contacts.")
        return

    for key, contact in sorted(contacts.items()):
        if not isinstance(contact, dict):
            continue
        print(f"{key} | {contact.get('name', '')} | {contact.get('company', '')} | {contact.get('role', '')}")


def show_status() -> None:
    ensure_dirs()
    print(f"lmstudio_base_url={LMSTUDIO_BASE_URL}")
    print(f"resolved_model={resolve_model()}")
    print(f"companies={len(list(COMPANIES.glob('*.txt')))}")
    print(f"contacts_file={CONTACTS_INDEX}")
    print(f"emails_log={EMAILS_LOG}")


def main() -> None:
    parser = argparse.ArgumentParser(description="CommerceGov local lead ops assistant")
    sub = parser.add_subparsers(dest="cmd", required=True)

    ingest_parser = sub.add_parser("ingest", help="Ingest raw .txt file")
    ingest_parser.add_argument("path")

    draft_parser = sub.add_parser("draft", help="Draft outreach email")
    draft_parser.add_argument("--company", required=True)
    draft_parser.add_argument("--goal", required=True)

    sub.add_parser("list-companies", help="List company records")
    sub.add_parser("contacts", help="List contacts")
    sub.add_parser("status", help="Show local configuration and ledger status")

    args = parser.parse_args()

    try:
        if args.cmd == "ingest":
            ingest_file(Path(args.path))
        elif args.cmd == "draft":
            draft_email(args.company, args.goal)
        elif args.cmd == "list-companies":
            list_companies()
        elif args.cmd == "contacts":
            show_contacts()
        elif args.cmd == "status":
            show_status()
    except LeadOpsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
