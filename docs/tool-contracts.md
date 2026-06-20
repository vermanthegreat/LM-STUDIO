# Typed Tool Contracts

## Tool policy

Every tool is registered in application code. A model can select only a
registered name and propose arguments matching its Pydantic input model.
Unknown fields are rejected. Tool handlers receive an application service and
unit of work, never credentials or a general SQL connection.

Risk classes:

- `read`: no state change; execute immediately.
- `propose`: stores candidates or a review proposal; normally execute.
- `write`: bounded reversible mutation; show confirmation when user intent is
  not explicit.
- `bulk_write`: changes multiple records; always require approval with count.
- `destructive`: merge, reject, archive, or remove; always require approval.
- `external`: accesses an enabled external provider; require explicit policy
  and preserve evidence.

## Foundation tools

### `search_contacts`

Read-only filters: text, organization status, tags, minimum relevance,
contact kind, verification state, has-person, has-open-task, limit, offset.
Returns identifiers, display fields, matched conditions, total count, and
provenance summary. Arbitrary SQL/filter expressions are forbidden.

### `find_companies_missing_email`

Read-only. Filters by status, tag, and minimum relevance. "Missing" must be
parameterized as no email, no non-rejected email, or no verified email. The
response states which definition was used.

### `list_due_followups`

Read-only. Accepts date boundary, status, priority, and limit. Dates are
interpreted in the configured local timezone and returned with timezone.

### `calculate_pipeline_analytics`

Read-only. Supports a fixed metric enum such as organization count, contact
coverage, verified-email coverage, relevance distribution, and overdue-task
count. Calculations are deterministic application queries.

### `create_task`

Write. Requires a target, title, optional due date, priority, and idempotency
key. Ambiguous targets or dates must be clarified. Bulk task creation requires
approval showing targets and dates.

### `save_discovered_contact`

Propose/write. Requires owner, kind, value, source, confidence, verification
state, and discovery method. A model assertion alone cannot produce `verified`
status. Conflicts create review candidates instead of overwriting values.

### `approve_extraction` / `reject_extraction`

Write. Operates on a specific proposed extraction version. Approval applies a
validated diff atomically. Rejection preserves source and proposal history.

### `merge_duplicate_contacts`

Destructive. Requires canonical and duplicate identifiers, a preview of moved
relationships, explicit approval, and transaction-level audit. Never infer a
merge from name similarity alone.

### `list_unverified_contact_methods`

Read-only. Filters by kind, source, age, relevance, and verification state.

## Planner output

The planner produces one of:

```json
{
  "action": "tool",
  "tool_name": "find_companies_missing_email",
  "arguments": {"minimum_relevance": 70, "missing_definition": "verified"},
  "reason": "User requested high-relevance companies without verified email."
}
```

```json
{
  "action": "clarify",
  "question": "Do you mean no email at all, or no verified email?"
}
```

The model cannot define new tools, approval policy, SQL, or executable code.

## Result envelope

All tools return:

- `tool_name`
- `status`
- `summary`
- `records` or `proposal`
- `record_count`
- `warnings`
- `provenance`
- `command_id`

The communication layer may shorten wording but cannot change identifiers,
counts, dates, verification states, or warnings.

