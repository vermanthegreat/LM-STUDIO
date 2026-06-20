# CommerceGov Contacts Agent Instructions

This repository builds a local-first contact-intelligence assistant served on
`127.0.0.1:8025`. These instructions apply to every file in the repository.

## Read before changing code

Use these documents as the authoritative specification:

1. `docs/product.md`
2. `docs/architecture.md`
3. `docs/data-model.md`
4. `docs/tool-contracts.md`
5. `docs/safety-and-communication.md`
6. `docs/implementation-roadmap.md`

If code and documentation disagree, do not silently preserve the discrepancy.
Identify it, then implement the smallest change consistent with the current
roadmap phase.

## Non-negotiable boundaries

- The local LLM is a planner and extractor, not a database administrator.
- Never give an LLM unrestricted SQL, shell, filesystem, network, or email
  capabilities.
- Never execute model-produced write SQL.
- Database reads and writes occur only through typed application services.
- Validate tool arguments independently of the model.
- Require explicit approval for consequential, ambiguous, bulk, destructive,
  or externally visible writes.
- Store provenance for extracted and discovered facts.
- Do not claim that a contact method is verified unless verification evidence
  exists in the database.
- Do not add generic web scraping, autonomous browsing, email sending, n8n, or
  SaaS multi-tenancy unless a later specification explicitly authorizes it.
- Tests must never use or mutate the real `leads.db` or production PostgreSQL.

## Engineering workflow

1. Inspect current behavior and working-tree changes.
2. State the roadmap phase and acceptance criteria affected by the task.
3. Make the smallest coherent patch; preserve unrelated user changes.
4. Add or update tests for behavior changes.
5. Run targeted tests, then the full suite when available.
6. Update documentation when contracts, configuration, or behavior change.
7. Report changed behavior, verification performed, and remaining limitations.

Do not combine the immediate SQLite hardening phase with PostgreSQL migration
or the general tool-planning loop. Each phase must leave the application
working and testable.

## Code standards

- Python 3.11+ with type hints on public interfaces.
- Pydantic models at HTTP, extraction, and tool boundaries.
- Explicit transaction ownership; one command equals one transaction unless a
  documented workflow requires otherwise.
- Structured logging without raw pasted text, credentials, or private contact
  data by default.
- Deterministic application validation before and after LLM calls.
- Stable error codes for programmatic clients and concise messages for users.
- Idempotency for retried mutations where duplicates would be harmful.
- Migrations are explicit and reversible; no schema mutation at import time.

## Assistant response behavior

- Lead with the database-grounded answer.
- State filters, counts, and important omissions.
- Separate facts, model inference, and proposed action.
- Cite source/provenance when presenting contact information.
- Before an approval-required write, show exactly what will change.
- After a write, report committed changes and the command-log identifier.
- Never represent missing data as negative evidence.

