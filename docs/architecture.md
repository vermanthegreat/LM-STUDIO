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

Secrets never belong in `.env.example`, logs, prompts, or committed fixtures.

## Trust boundaries

- Raw pasted text is untrusted content and can contain prompt injection.
- LLM output is untrusted structured input.
- Tool arguments are untrusted until application validation succeeds.
- External source content is evidence, not automatically verified truth.
- Database constraints are the final integrity boundary.

