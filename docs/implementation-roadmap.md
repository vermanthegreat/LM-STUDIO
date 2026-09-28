# Implementation Roadmap

Each phase is independently releasable. Do not begin a later phase while the
current phase acceptance criteria remain unmet.

## Current repository state

As of `main` (commit containing Phase 0 hardening plus in-tree Phase 1
foundation code):

- **Phase 0** (SQLite hardening) is implemented and is the active reviewed
  foundation for local use on `127.0.0.1:8025` with SQLite as the default
  runtime.
- **Phase 1 foundation code may exist in-tree** (`persistence/`, `repositories/`,
  Alembic baseline, migration CLI, optional `DATABASE_URL` path). That presence
  does **not** mean Phase 1 is accepted.
- **Phase 1 is not accepted** until every Phase 1 acceptance criterion below
  is verified (including repeatable migration with reconciliation report and
  isolated PostgreSQL integration tests).

Agents must still execute phases separately. Do not combine Phase 0 review work
with Phase 1 certification, PostgreSQL cutover, or Phase 2+ tool planning in a
single patch.

## Phase 0 — immediate SQLite hardening

**Status:** Implemented in-tree; subject to verification and review. Not
combined with PostgreSQL migration or agent tool planning.

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

**Status:** Foundation code may exist in-tree; **not accepted** until explicit
acceptance criteria below pass and are reported.

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

## Phase G0 — Gmail read-only intake

**Status:** Implemented in-tree for SQLite runtime; PostgreSQL schema migration
added (`004_gmail_g0`); operator sync and query surfaces available on port 8025.
PostgreSQL Gmail operations fail closed with `gmail_postgresql_runtime_unsupported`.

Purpose:

- import explicitly selected Gmail conversations from a configured label;
- preserve provider provenance in local `Source`/`Interaction`-aligned tables;
- determine direction and high-level message intent;
- mark messages requiring operator attention with validated markers;
- connect messages to existing contacts only when identity is deterministic;
- keep uncertain linkage reviewable;
- make imported communication queryable through typed read tools and `/ask`.

Explicitly out of scope for G0:

- Gmail sending, drafts, label changes, or any inbox mutation;
- background polling or push notifications;
- Google Calendar OAuth or event reads/writes;
- automatic task or lead creation from email markers;
- bulk historical inbox ingestion beyond `GMAIL_SYNC_LIMIT`;
- MCP or LLM-direct Gmail access.

Acceptance highlights:

- OAuth requests only `https://www.googleapis.com/auth/gmail.readonly`.
- Manual `POST /integrations/gmail/sync` with command-log audit entry.
- Idempotent local persistence with provider/account/message uniqueness.
- Tests use `FakeGmailProvider` only.

## Phase K0 — local knowledge ingestion and retrieval

**Status:** Implemented in-tree for the SQLite runtime. See `docs/knowledge.md`.

Scope: drag-and-drop file ingestion on `/knowledge`, content-addressed
original preservation, deterministic type routing and extraction (text, PDF,
DOCX), optional vision provider for images, schema-validated advisory LLM
classification, relational topics/entities with exact-only contact linking,
FTS5 search with metadata filters, `search_knowledge` read tool, and an
evidence-cited `/ask` path.

Out of scope for K0: embeddings/vector search, PostgreSQL knowledge
persistence, scanned-PDF OCR, editing or deleting items, and automatic
consolidation or contradiction resolution.

Acceptance highlights:

- Exact duplicate files are detected by SHA-256 and never stored twice.
- Per-file failure isolation; model failure keeps the original and extracted text.
- Model output cannot corrupt storage (Pydantic validation; invalid output is recorded, not persisted as metadata).
- No fabricated vision output; unavailable vision is explicit.
- Tests use `tmp_path` databases and storage only.

## Phase K1 — local embeddings and hybrid knowledge retrieval

**Status:** Canonical specification. Implemented on branch
`claude/phase-k1-hybrid-retrieval`; not merged until reviewed.

Objective:

Add locally generated embeddings and hybrid lexical/semantic retrieval to the
K0 knowledge subsystem so the application can find relevant business context
even when the query and source use different wording, while preserving
deterministic lexical retrieval, source attribution, local-first operation,
and safe fallback behavior.

Baseline note: K0 indexes whole knowledge items and has no chunk table. K1
introduces deterministic, fingerprinted chunks of each item's extracted text
as the unit of embedding. Citations remain item-level (`[K<id>]`) and carry
the matching chunk identity.

In scope:

- configurable local embedding provider (OpenAI-compatible `/v1/embeddings`,
  as served by LM Studio), disabled by default;
- endpoint, model, timeout, batch size, and enabled/disabled configuration;
- embedding generation for chunks of supported K0 items;
- persistent embedding storage in the existing SQLite knowledge database via
  an additive schema migration;
- embedding model identity and vector-dimension tracking;
- semantic (cosine) similarity search over compatible embeddings;
- hybrid ranking combining K0 lexical results with semantic results using
  Reciprocal Rank Fusion;
- deterministic ranking and tie-breaking;
- stable source citations;
- indexing status and failure visibility;
- bounded re-indexing of existing K0 items, including retry of failures;
- lexical-only fallback when embeddings are disabled, unavailable, or invalid;
- tests, configuration documentation, and operating instructions.

Explicitly out of scope:

- PostgreSQL persistence;
- editing or deleting knowledge items;
- manual or automatic re-classification;
- OCR for scanned PDFs;
- automatic consolidation;
- contradiction resolution;
- cloud-hosted vector databases;
- autonomous external data collection;
- email sending, receiving, synchronization, or account management;
- K2 or later phases.

Acceptance:

- K0 lexical retrieval and existing K0 data keep working before and after
  migration; basic K0 operation never requires embeddings.
- Semantic retrieval returns relevant results without exact keyword overlap.
- Hybrid results are deterministic, deduplicated per item, and cite only
  retrieved items and chunks.
- Provider failure, disabled configuration, model change, or dimension
  mismatch is explicit and falls back to lexical retrieval.
- Unchanged chunks are never re-embedded; changed chunks are.
- The default test suite uses deterministic fake embeddings and needs no live
  service, network, model download, or real data.

## Phase G1 — temporal follow-up reasoning (future)

- Resolve controlled date/time expressions using `APP_TIMEZONE`.
- Create task proposals from email markers with explicit approval.
- Maintain overdue/upcoming views.

## Phase G2 — Gmail production readiness and multi-account

**Status:** Canonical specification; not implemented. Branch naming:
`claude/phase-g2-*`. G2 does not depend on G1; G1 remains future.

Objective:

Make the Gmail subsystem a correct, restart-safe email transport and state
foundation for daily business use across **four Gmail accounts**:
reliable sync and reading, correct thread reconstruction, durable drafts,
replies and forwards, and **explicit operator-initiated send** with send
results reconciled from Gmail. G2 is transport and state only; AI drafting and
K1-powered contextual writing are a later, separate phase.

Policy authorization: `AGENTS.md` forbids email sending "unless a later
specification explicitly authorizes it". This section is that specification,
and it authorizes **only** operator-initiated sends of an operator-reviewed
draft through the typed application path. The LLM never receives send,
draft-write, or Gmail capabilities. When G2 ships, `docs/product.md`
("Excluded scope"), `docs/safety-and-communication.md` ("Email sending is not
supported"), and `docs/architecture.md` (Gmail provider boundary, read-only
scope) must be updated in the same change to match.

Current baseline (audited on `main` @ `354b279`):

- Exists (G0): a single account through one `GMAIL_TOKEN_PATH`;
  `gmail.readonly` scope only; manual label-scoped sync
  (`GMAIL_SYNC_LABEL`, first page only, `GMAIL_SYNC_LIMIT`, no page loop);
  `gmail_sources` unique on `(provider, external_account,
  external_message_id)`; per-message classification and exact-email contact
  linking; RFC `Message-ID` persisted; `/emails`, `/emails/thread/{id}`,
  `/integrations/gmail`; read tools `list_email_messages` and
  `get_email_thread`; `FakeGmailProvider` tests in `tests/test_gmail_g0.py`;
  SQLite only (PostgreSQL fails closed).
- Partial: `In-Reply-To`, `References` and `labelIds` are parsed in
  normalization but not persisted as queryable state. Bodies are decoded as
  UTF-8 regardless of the declared charset. HTML is flattened to text only
  when no text/plain part exists. Attachments are recorded as metadata only.
  Token refresh happens at load, with a non-atomic token-file write.
  `gmail_sync_state` is a single global row (`CHECK (id = 1)`), not
  per-account.
- Missing: multiple accounts; full-mailbox sync on `main`;
  `historyId`-based incremental sync; resume cursors; deletion, archive, spam
  and trash convergence; per-account health; retry, backoff and rate-limit
  handling; revoked-token handling; drafts; reply, reply-all and forward; any
  send path; attachment download or upload; live Gmail certification
  evidence (none exists in the repository).
- Unmerged prior work: branch `origin/agent/contact-intelligence-hardening`
  (diverged at `8528eec`, before K0) contains related Gmail commits (for
  example `ecd9a29`, `816bbb0`, `3f1c4fd`, `06e04c0`):
  - per-account `gmail_mailbox_sync_state` with page-token resume and a
    `latest_history_id` column;
  - `includeSpamTrash` paging;
  - atomic token writes;
  - `gmail_conversations` and `lead_communication_state` projections;
  - related tests.

  It remains read-only and single-token. **Gate G2-0:** before G2
  implementation starts, the operator decides whether those commits are
  integrated into `main` (reviewed, conflict-resolved against K0/K1) or
  superseded. G2 must not silently duplicate or discard them.

In scope:

1. **Multi-account**
   - Up to four independently configured Gmail accounts, each with its own
     OAuth token file and enabled flag.
   - Account identity (normalized account email) is a required, immutable
     key on every persisted mailbox object: sources, messages, threads,
     labels/state, sync cursors, drafts, send attempts, and command-log
     arguments.
   - Every mailbox query, tool, and route is account-scoped or explicitly
     multi-account with account shown.
   - No cross-account message, thread, draft, or send leakage.
2. **Mailbox sync**
   - Per account: full sync via `messages.list` paging to exhaustion, with a
     persisted page cursor.
   - Incremental sync via `users.history.list` from the stored `historyId`.
   - When the stored `historyId` is too old or invalid (HTTP 404), a
     recorded fallback to a bounded full re-sync.
   - Restart/resume from the last committed cursor.
   - Deduplication and idempotent replay of the same page or history data.
   - Convergence of label, archive, trash, spam, and deletion state from
     Gmail.
3. **Thread correctness**
   - Gmail `threadId` is authoritative for thread membership.
   - `Message-ID`, `In-Reply-To` and `References` are persisted per
     message.
   - Reply, reply-all and forward drafts set `threadId`, `In-Reply-To`,
     `References`, and the subject prefix correctly.
   - Reply-all derives recipients from the parent's
     From/Reply-To/To/Cc, removes the sending account's own addresses, and
     deduplicates case-insensitively.
   - `Bcc` is never exposed from received mail and never propagated.
4. **MIME and content**
   - text/plain and text/html (sanitized for display; raw HTML is never
     rendered unsanitized).
   - Declared charsets, with a recorded replacement on invalid bytes.
   - multipart/alternative, multipart/mixed and multipart/related.
   - Quoted-text and signature detection, used for display only; the stored
     original body is unchanged.
   - Attachments and inline (`Content-ID`) images, fetched on demand with
     size limits.
   - Common malformed Gmail messages (missing charset, bad part headers,
     empty bodies) degrade without failing the sync.
5. **Drafts and send**
   - Create, edit, save and reload drafts, including reply, reply-all and
     forward drafts and drafts with attachments.
   - Drafts are persisted locally, bound to one account, and mirrored to
     Gmail Drafts (`drafts.create` / `drafts.update`).
   - Send happens only through an explicit operator action on a specific
     draft version (a typed route with loopback/Origin protection and a
     command-log entry). The planner, `/ask`, and LLM paths cannot send.
   - Each send carries an idempotency key, so a retried send never sends
     twice.
   - Send results are reconciled from Gmail (message id, `threadId`, `SENT`
     label via fetch/sync) before being shown as sent. A send is never
     assumed.
6. **Auth and reliability**
   - OAuth scopes limited to `gmail.readonly` plus `gmail.compose`
     (drafts and send). No `gmail.modify` and no full-mailbox scope unless a
     later specification requires it.
   - Per-account re-authorization; atomic token refresh and write.
   - Expired, revoked (`invalid_grant`) and insufficient-scope credentials
     are detected and shown per account, and never corrupt mailbox state.
   - Gmail API errors are classified. Rate limits (429 and 403
     `rateLimitExceeded`/`userRateLimitExceeded`) and 5xx responses get
     bounded exponential backoff with a retry cap.
   - One account's failure never blocks the other accounts.
   - Safe restart at any point.
7. **Observability**
   - Per-account status: enabled, auth state, last successful sync, last
     attempt, last error code, current sync mode and cursor/`historyId`,
     counts, failed operations, pending/failed drafts and sends.
   - An operator-visible recovery action per state (re-authorize, resume,
     full re-sync, retry failed item).
   - The command log records counts and identifiers, never bodies or
     tokens.

Out of scope:

- autonomous email sending, or any automatic approval to send;
- AI-generated or K1-contextual drafting (later separate phase);
- autonomous follow-up actions;
- Calendar integration (C0/C1);
- scraping, browser automation, new external data collection;
- advanced semantic email retrieval;
- K1 redesign;
- CRM redesign; contact-intelligence expansion unrelated to email
  transport/state;
- agent orchestration;
- any cloud LLM dependency;
- PostgreSQL Gmail persistence (SQLite stays the supported runtime; the
  PostgreSQL runtime keeps failing closed);
- UI redesign beyond what multi-account and health visibility require.

Invariants:

- `(account, external_message_id)` and `(account, external_thread_id)` are
  unique. The same Gmail ids in two accounts are distinct records.
- A sync cursor advances only after the data it covers is committed.
- Replaying any committed page or history batch changes nothing.
- Local sent state exists only after Gmail reconciliation.
- A draft or send belongs to exactly one account and can only reference
  that account's threads.
- Tokens and message bodies never appear in logs, the command log, prompts,
  or the repository.
- Tests never contact Gmail and never read real credentials or mailboxes.

Acceptance criteria:

- A. Four accounts can be configured, authorized, enabled and disabled
  independently.
- B. Identical Gmail message/thread ids in different accounts cannot
  collide in storage, queries, UI or tools.
- C. A full sync followed by an incremental sync creates no duplicates.
- D. A sync interrupted mid-page or mid-history resumes from the last
  committed cursor without gaps or duplicates.
- E. Re-running the same page or history data is idempotent.
- F. Replies and forwards preserve the intended Gmail thread membership and
  headers.
- G. Reply-all produces the correct recipients, never includes the sending
  account itself, and never duplicates an address.
- H. Draft create/edit/reload survives a process restart.
- I. Sending requires an explicit operator action on a specific draft
  version; no other path can send.
- J. A successful send is reconciled from Gmail (message id, thread,
  `SENT`) rather than assumed; an ambiguous outcome is surfaced, not retried
  blindly.
- K. Attachments and common multipart MIME cases round-trip correctly
  (receive, display, draft, send).
- L. OAuth expiration or revocation is visible per account and does not
  corrupt mailbox state.
- M. One account's failure does not block sync of the others.
- N. Automated tests prove account isolation, pagination invariants,
  restart/resume, history replay idempotency, and send idempotency.
- O. A controlled live Gmail certification procedure is documented and
  executed only by the operator, outside automated tests.

Test requirements:

- A deterministic fake Gmail provider extending `FakeGmailProvider`, with
  multiple accounts, paging, history records (including expired
  `historyId`), label changes and deletions, drafts, send, injected errors
  (401, 403 scope, `invalid_grant`, 404 history, 429, 5xx, timeouts), and
  crash injection between fetch and commit.
- MIME fixtures built in-test (plain, HTML-only, alternative, mixed with
  attachments, related with inline images, non-UTF-8 charsets, malformed
  parts).
- Isolation tests run the same ids in two accounts through every route and
  tool.
- The existing G0, K0 and K1 suites stay green; temporary databases only;
  the default `leads.db` is untouched; no network.

Live certification (operator-run, not automated):

1. Use disposable or test Gmail accounts first, then the four business
   accounts.
2. Authorize each account with the documented scopes.
3. Run a full sync, then incremental sync after new mail arrives; check the
   counts and that there are no duplicates.
4. Kill the process mid-sync and restart; confirm resume.
5. Revoke one account's token; confirm the others keep syncing and the
   revoked account shows re-authorize.
6. Create a reply draft, restart, reload, edit, then explicitly send to an
   operator-controlled address.
7. Confirm in Gmail and in local state that the message is in the correct
   thread with correct headers and recipients, and that there is no
   duplicate or cross-account state.
8. Record dates, accounts (redacted), counts and results in a certification
   note. No credentials, bodies or personal data are committed.

Completion definition (end-to-end target):

Mail arrives on one of four Gmail accounts
→ incremental sync ingests it
→ it is associated with the correct account, thread, person and company
→ the message is readable
→ the operator creates a reply draft
→ the draft survives reload and restart
→ the operator explicitly sends
→ Gmail confirms the sent message in the correct thread
→ local state re-syncs from Gmail
→ no duplicate or cross-account state exists.

G2 is complete only when criteria A–N pass in automated tests, gate G2-0 has
been resolved, the policy documents above are updated, and the live
certification (O) has been executed and recorded by the operator.

## Phase C0 — Google Calendar read-only context (future)

- Separate Calendar OAuth scope and authorization.
- Read operator availability and existing events.
- Correlate meeting requests with availability.
- No event writes.

## Phase C1 — governed Calendar proposals (future)

- Propose event title, participants, start/end, timezone, source thread.
- Operator review and explicit approval before isolated Calendar writes.

