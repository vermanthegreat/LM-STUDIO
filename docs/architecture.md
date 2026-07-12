# Architecture

## Request flow

```text
Browser or local API client on :8025
              |
              v
FastAPI routes and Pydantic validation
              |
              v
Command service / extraction service
              |
        +-----+----------------+
        |                      |
        v                      v
Local LLM adapter        Typed tool registry
(structured output)      (application-owned)
        |                      |
        +----------+-----------+
                   v
          Repository / unit of work
                   |
                   v
            PostgreSQL + audit
```

The LLM may propose an extraction or tool call. It cannot execute a tool,
construct a database session, or bypass application validation.

## Components

### FastAPI application

- App factory with explicit configuration.
- Lifespan initialization; no deprecated startup event.
- HTML routes for local use and versioned JSON endpoints for internal clients.
- Loopback binding and minimum Origin/Host protection.
- Stable validation and error responses.

### Local LLM adapter

- One interface supporting LM Studio and Ollama-compatible chat endpoints.
- Configurable endpoint, model, timeout, and structured-output strategy.
- No database credentials in prompts.
- Model output parsed into Pydantic types; malformed output is rejected or sent
  to review, never trusted implicitly.
- Deterministic fallback may extract obvious data but must label confidence.

### Application services

- `IntakeService`: validates raw input and coordinates extraction.
- `ExtractionService`: calls the model and validates proposed facts.
- `ReviewService`: approves or rejects proposed extraction changes.
- `CommandService`: resolves user intent, selects tools, handles approvals, and
  writes command-log entries.
- `ContactService`: manages organizations, people, contact methods, and
  deduplication.
- `TaskService`: manages local follow-up plans.
- `AnalyticsService`: deterministic, read-only metrics.

### Typed tool registry

Each tool has a unique name, Pydantic input/output schema, risk class,
authorization/approval policy, handler, and audit behavior. Tool handlers call
application services, not raw model-produced SQL.

### Persistence

- PostgreSQL target with SQLAlchemy 2.x and Alembic.
- Repository interfaces and request/command-scoped unit of work.
- Exactly one commit per mutating command.
- Rollback on validation, extraction, handler, or audit failure.
- Constraints enforce ownership, uniqueness, valid states, and relationships.

### Command lifecycle

1. Normalize and log the user command as `received`.
2. Resolve deterministic intents before consulting the LLM.
3. Ask the LLM for one typed plan when needed.
4. Validate plan and arguments against the registered tool schema.
5. Reject unknown tools and unsupported filters.
6. For approval-required operations, persist a proposal without executing it.
7. Execute approved/read-only tools in an application-owned transaction.
8. Validate the result and record counts, identifiers, and errors.
9. Produce a grounded response; optional LLM wording cannot alter facts.

## Configuration

Configuration must be environment-driven and validated at startup:

- `APP_HOST=127.0.0.1`
- `PORT=8025`
- `DATABASE_URL`
- `LMSTUDIO_BASE_URL`
- `LMSTUDIO_MODEL`
- `LMSTUDIO_TIMEOUT`
- `MAX_PASTE_CHARS`
- `LOG_LEVEL`
- `GMAIL_ENABLED` — enable Phase G0 Gmail read-only intake (default `false`)
- `GMAIL_CLIENT_SECRET_PATH` — path to Google OAuth desktop client JSON
- `GMAIL_TOKEN_PATH` — path to authorized-user token file (gitignored)
- `GMAIL_SYNC_LABEL` — operator-created Gmail label to sync (default `LMStudio`)
- `GMAIL_SYNC_LIMIT` — maximum messages per manual sync (default `100`)
- `APP_TIMEZONE` — IANA timezone for follow-up dates and email display

Secrets never belong in `.env.example`, logs, prompts, or committed fixtures.

## Gmail provider boundary (Phase G0)

Gmail G0 is a bounded read-only intake path:

```text
Operator UI  POST /integrations/gmail/sync
       |
       v
GmailProviderAdapter  (gmail.readonly OAuth only)
       |
       v
normalize + classify + link  (application services)
       |
       v
SQLite gmail_* tables  (supported runtime)
```

- **OAuth:** `scripts/gmail_authorize.py` bootstraps desktop OAuth with exactly
  `https://www.googleapis.com/auth/gmail.readonly`. Token files are gitignored
  and never passed to the LLM.
- **Manual sync:** Operator triggers bounded label sync from
  `/integrations/gmail`. Each sync writes a `gmail_sync` command-log entry with
  counts only (no tokens or full message bodies).
- **Fake provider:** Automated tests use `FakeGmailProvider` only; it is not
  reachable from production routes.
- **Classifier trust boundary:** Email bodies are untrusted. The local LLM
  classifier returns schema-validated intent/markers only; it cannot invoke
  tools, Gmail, or database writes. `requires_followup` is derived from
  validated markers.
- **PostgreSQL capability:** Alembic migration `004_gmail_g0` defines PostgreSQL
  schema, but Gmail sync and query operations are **SQLite-only** in G0.
  PostgreSQL runtime requests fail closed with
  `gmail_postgresql_runtime_unsupported` rather than returning empty results.
- **`/ask` reads:** `list_email_messages` and `get_email_thread` read the local
  database only; they do not call Gmail during ordinary `/ask` queries.

## Trust boundaries

- Raw pasted text is untrusted content and can contain prompt injection.
- LLM output is untrusted structured input.
- Tool arguments are untrusted until application validation succeeds.
- External source content is evidence, not automatically verified truth.
- Database constraints are the final integrity boundary.

