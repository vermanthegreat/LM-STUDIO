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

