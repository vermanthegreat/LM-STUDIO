# Minimum Contact Data Model

This is the target PostgreSQL model. It is documented now and implemented only
after the SQLite hardening phase.

## organizations

Identity and relevance for a company or agency:

- `id` UUID primary key
- `name`, `normalized_name`
- `website`, `normalized_domain`
- `description`
- `status`
- `relevance_score` and `relevance_reason`
- `created_at`, `updated_at`, `archived_at`

Names are not unique. Domain and corroborating source facts drive duplicate
detection; merges remain explicit and auditable.

## people

- `id` UUID primary key
- `organization_id` nullable foreign key
- `name`, `normalized_name`, `title`
- `is_decision_maker`
- `relevance_score`, `relevance_reason`
- timestamps

People may temporarily exist without a known organization.

## contact_methods

- `id` UUID primary key
- `organization_id` nullable foreign key
- `person_id` nullable foreign key
- `kind`: email, phone, linkedin, website, other
- `value`, `normalized_value`
- `source_id` nullable foreign key
- `source_url`
- `confidence` in `[0, 1]`
- `verification_status`: unverified, syntax_valid, source_confirmed, verified,
  rejected, stale
- `is_primary`
- `discovered_at`, `verified_at`, timestamps

A check constraint requires exactly one of `organization_id` and `person_id`.
This physically implements the conceptual `owner_type / owner_id` requirement
while retaining foreign-key integrity.

Email, phone, and profile URLs must not exist only as columns on organization
or person records. Separate records support multiple values, provenance,
confidence, verification, conflict resolution, and history.

## sources

- `id` UUID primary key
- `source_type`
- `source_url`
- `raw_text` or protected content reference
- `content_hash`
- `captured_at`
- `created_by`

Identical hashes can prevent accidental duplicate intake without asserting
that two sources are semantically equivalent.

## extractions

- `id`, `source_id`
- `model`, `prompt_version`
- `status`: pending, proposed, approved, rejected, failed
- `confidence`
- `structured_output` JSONB
- `error_code`, `error_message`
- `approved_at`, `rejected_at`, timestamps

The proposed output is retained separately from canonical contact records.

## interactions

- `id`
- optional `organization_id` and `person_id`
- `kind`, `occurred_at`, `summary`
- optional `source_id`
- `requires_followup`
- timestamps

## tasks

- `id`
- optional `organization_id` and `person_id`
- `title`, `description`
- `status`, `priority`, `due_at`, `completed_at`
- optional `created_by_command_id`
- timestamps

## tags

- `tags(id, name, normalized_name)`
- `organization_tags(organization_id, tag_id)`
- `person_tags(person_id, tag_id)`

## command_log

- `id`
- `command_text`
- `intent`, `tool_name`, `tool_arguments` JSONB
- `risk_class`
- `status`: received, planned, awaiting_approval, executing, succeeded,
  rejected, failed
- `requires_approval`, `approved_at`
- `result_summary` JSONB
- `error_code`, `error_message`
- `correlation_id`, timestamps

Avoid storing complete prompts or private raw source text in ordinary logs.

## Required invariants

- Contact method ownership is exactly one organization or person.
- Confidence is between zero and one.
- `verified_at` is present only for verified states.
- At most one primary contact per owner and contact kind where practical.
- Canonical records are updated only through an approved extraction or typed
  application command.
- Merge operations preserve redirects/history and never silently delete source
  evidence.

## Gmail G0 tables (Phase G0 — SQLite runtime; PostgreSQL schema only)

Gmail intake stores provider provenance separately from canonical contact
records. Imported messages are local copies for review and `/ask` queries;
they do not mutate the remote mailbox.

### gmail_sources

Immutable provider provenance for one Gmail message:

- `id` UUID primary key
- `provider` — fixed provider identity (`gmail`); immutable provenance
- `external_account` — connected Gmail account email
- `external_message_id` — Gmail API message ID
- `external_thread_id` — Gmail thread ID
- `external_rfc_message_id` — RFC `Message-ID` when present
- `provider_occurred_at` — provider timestamp (stored in UTC)
- `content_hash` — hash of normalized body/metadata for change detection
- `provider_metadata` — JSON attachment/header metadata (not OAuth tokens)
- `raw_text` — normalized plain-text body for local review
- `created_at`, `updated_at`

**Uniqueness / idempotency:** `(provider, external_account, external_message_id)`
is unique. Retried syncs upsert on this boundary; duplicates are not created.

### gmail_messages

Application-owned interpretation and linkage for one imported message:

- `id` UUID primary key
- `gmail_source_id` foreign key to `gmail_sources` (cascade delete)
- `organization_id` nullable FK to `organizations` (legacy `lead_id` in SQLite)
- `person_id` nullable FK to `people`
- `subject`, `direction` (`inbound` / `outbound` / `internal` / `unknown`)
- `occurred_at` — canonical message time in UTC
- `from_address`, `to_addresses`, `cc_addresses` — JSON address lists
- `primary_intent` — validated enum (`positive_interest`, `automated`, etc.)
- `intent_confidence` — `[0, 1]`
- `classification_source` — `deterministic`, `local_llm`, or `fallback`
- `classification_model`, `classification_warning` — optional classifier metadata
- `markers` — JSON array of validated attention markers (fixed enum)
- `temporal_signals` — JSON array of unresolved/ambiguous/resolved time phrases
- `link_status` — `linked`, `unlinked`, or `ambiguous`
- `requires_followup` — derived from validated markers (not model free-text)
- `created_at`, `updated_at`

### gmail_sync_state

Singleton operator sync status (one row, `id = 1`):

- `account_email` — last connected account
- `configured_label` — operator-created Gmail label name (default `LMStudio`)
- `last_sync_at` — last attempt timestamp (UTC)
- `last_success_at` — last fully successful sync (UTC); not advanced on error
- `last_status` — `ok`, `partial`, or `error`
- `last_result_summary` — JSON counts (imported, failed, linked, etc.)
- `last_error_code` — safe application error code (no secrets)

### Gmail G0 invariants

- Gmail provider identity is immutable provenance; re-import does not change
  provider/account/message identity.
- `(provider, external_account, external_message_id)` is the idempotent import
  boundary.
- Domain-only or website evidence cannot auto-link a message to a contact.
- Gmail import cannot automatically create a lead, person, task, or Calendar
  event.
- Markers use the fixed `AttentionMarker` enum; unknown values are rejected.
- Datetimes are stored canonically in UTC; `APP_TIMEZONE` is used only for
  display and controlled temporal interpretation.
- Ambiguous temporal phrases (e.g. "next week") are persisted as unresolved or
  ambiguous signals, not guessed into concrete deadlines.

