# Phase 0 Implementation Prompt

```text
Implement Phase 0 from docs/implementation-roadmap.md in this repository.

Read AGENTS.md and all authoritative docs first. Preserve unrelated working-tree
changes. Keep SQLite for this phase and do not implement PostgreSQL, SQLAlchemy,
general agent planning, scraping, discovery, email, or notifications.

Required outcome:
- create_app(config) and FastAPI lifespan
- module-level app compatible with uvicorn
- default 127.0.0.1:8025
- configurable test database path
- strict source enum, trimmed non-empty raw text, configurable maximum length,
  HTTP/HTTPS URL validation, and existing attached-lead validation
- controlled validation and extraction/database errors
- one SQLite transaction covering all writes from one parse request
- rollback on every injected persistence failure
- minimum Origin/Host protection for writes
- real 404 for missing leads
- encoded 303 success redirects
- CSV formula hardening for =, +, -, and @ prefixes
- pytest route tests using only temporary databases and mocked LLM extraction
- README, requirements, and .env.example aligned with port 8025 and real env
  names, with placeholder credentials only

Acceptance criteria are exactly those in Phase 0. Run targeted and full tests.
Report changed files, test results, and remaining limitations.

Stop if atomic transaction support requires a schema redesign, if a test would
touch real leads.db, or if unrelated user changes cannot be preserved. Explain
the concrete conflict instead of broadening scope.
```

