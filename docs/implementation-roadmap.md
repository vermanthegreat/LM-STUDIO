# Implementation Roadmap

Each phase is independently releasable. Do not begin a later phase while the
current phase acceptance criteria remain unmet.

## Phase 0 — immediate SQLite hardening

Scope:

- FastAPI app factory and lifespan.
- Configurable isolated database path.
- Strict paste validation and maximum size.
- Allowed source-type enum and URL validation.
- Existing attached-lead validation.
- One atomic transaction for a parse request.
- Structured errors and safe logging.
- Loopback default plus Origin/Host protection.
- Real 404 responses and encoded redirects.
- CSV spreadsheet-formula hardening.
- Route tests, pytest dependencies, and documentation/config alignment.

Acceptance:

- Valid existing workflows remain functional on port 8025.
- Invalid input produces controlled 4xx responses.
- Injected failure at any persistence step leaves no partial records.
- Tests never access the real database.
- Missing leads return 404 and unsafe origins cannot mutate state.
- Full test suite passes.

Stop conditions:

- Do not redesign the schema or migrate PostgreSQL in this phase.
- If transaction propagation requires broad incompatible API changes, document
  the smallest unit-of-work boundary before proceeding.
- Do not delete user data to make tests pass.

## Phase 1 — PostgreSQL contact foundation

Scope:

- SQLAlchemy models matching `docs/data-model.md`.
- Alembic baseline and migrations.
- Repository interfaces and unit of work.
- Extraction proposal/review separation.
- Repeatable SQLite-to-PostgreSQL migration command with dry run.
- Data validation and reconciliation report.

Acceptance:

- Migration is repeatable, transactional, and reports every skipped/conflicting
  record.
- Contact methods retain source and verification state.
- Application routes use repositories rather than direct SQL.
- PostgreSQL integration tests run against an isolated test database.

Stop conditions:

- Do not reuse legacy `db_postgres.py` without reconciling its model and tests.
- Do not enable general command planning yet.

## Phase 2 — typed query and planning foundation

Scope:

- Tool registry, schemas, risk classes, and deterministic handlers.
- `command_log` state machine.
- Read tools from `docs/tool-contracts.md`.
- Deterministic intent routing followed by local LLM planning when required.
- Tool result envelope and grounded response formatter.

Acceptance:

- Unknown tools and fields are rejected.
- No planner output is executed as SQL or code.
- Every command has an auditable terminal or awaiting-approval status.
- Counts and filters in responses match deterministic repository results.

## Phase 3 — controlled writes and organization

Scope:

- Task creation, extraction approval/rejection, contact proposals, and duplicate
  merge preview.
- Approval tokens tied to immutable proposals.
- Idempotent write execution and rollback tests.

Acceptance:

- Bulk/destructive writes cannot execute without explicit approval.
- Retrying a command cannot duplicate tasks or contact methods.
- Every mutation reports exact committed identifiers and command ID.

## Phase 4 — optional enrichment

Only after explicit policy and provider selection:

- Narrow external contact discovery adapter.
- Per-provider rate limits and source capture.
- Candidate verification workflow.

External discovery must not become a generic autonomous browser. Email
handling, sending, notifications, and workflow automation remain separate
future decisions.

