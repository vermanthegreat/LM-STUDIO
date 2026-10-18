# Completion Specification — SQLite finalization scope (Phase F)

**Status:** Ratified scope specification for the finalization plan
(`docs/finalization/`, tasks T01–T32). Documentation only: it authorizes
*implementation work* within the bounds below; it does **not** certify any
gate, does not authorize any live provider action, and does not authorize
automated Community collection. Every capability below is unimplemented until
its task lands and its gate (see `docs/finalization/E-acceptance.md`) passes.

**HUMAN scope acknowledgement:** PENDING. T03's evidence requires the operator
to acknowledge this scope (section 10). Implementation tasks that depend on T03
(T06 onward) must not be promoted to `main` until it is recorded.

## 1. Relationship to other specifications

- SQLite remains the supported runtime. PostgreSQL Phase 1 remains **not
  accepted**; nothing here changes its gates. New completion schema uses
  explicit, versioned, additive SQLite migrations (T08); no schema DDL at
  import time, on read paths, or inside MCP reads.
- Existing phases (0, 2, 3, G0, K0, K1, CI M0/M2, G2) keep their own
  acceptance criteria and are independently gated. This document only adds
  scope and resolves the conflicts listed in section 2.
- `docs/implementation-roadmap.md` Phase G2 stays the canonical Gmail
  transport/state specification. This document supplies the "later, separate
  phase" for local-LLM drafting that G2 defers, and amends one G2 invariant
  (section 6).
- `docs/community.md` (Community branch, integrated at T07) stays the
  canonical Community capture specification, amended as in section 3.
- Where this document and an older document disagree on the scope it lists,
  this document wins; everything else in the older document stands.

## 2. Before/after policy table

| Topic | Before (source) | After (this spec) |
|---|---|---|
| Community MCP exposure | Prohibited until the collection authorization gate (`community.md` M0; roadmap CI M3+) | **Stored-data** Community reads may be exposed through the read-only MCP adapter (section 5) independently of collection. Collection authorization is unrelated to reads of data already stored. |
| Community classification | "No LLM is involved" (`community.md`); "LLM classification" listed as future | Local-model classification of **stored** posts into versioned pain-point records with post/span provenance is in scope (section 3). The model never navigates, fetches, or writes source rows. |
| Community → CRM links | "No lead, person, interaction or task is created or linked" | Links are created only as reviewed, operator-approved evidence associations (section 4). No automatic person/company creation from usernames. |
| Automated Community collection | Prohibited pending authorization | **Unchanged: prohibited.** Status: **AUTOMATED COLLECTION NOT AUTHORIZED** (section 3.4). Remains a required final capability subject to verified authorization; not implied by any other item. |
| Message bodies in prompts | G2 invariant: bodies never in prompts | Selected, bounded excerpts may go to the **configured local model only**, for classification and draft assistance (section 6). Never to cloud inference, logs, the command log, or MCP. |
| AI drafting | G2: out of scope (later separate phase) | Local-model **suggestions** for an operator-owned draft are in scope (section 7). Suggestion ≠ draft ≠ approval ≠ send. |
| Email sending | `safety-and-communication.md`: not supported; `product.md`: out of scope | Only as G2 specifies: explicit operator send of one approved, immutable draft snapshot through the typed route. No model, `/ask`, planner, or MCP path can send. Not implemented until T28. |
| Email follow-up tasks | Markers stored; no automatic task creation (G0) | Markers may produce a **follow-up task proposal** that the operator approves through the existing write-proposal path (section 8). Never an automatic task. |
| Candidate → canonical contact | Candidates are noncanonical; promotion incomplete | Governed, version-bound candidate promotion with preserved evidence (section 4). |
| MCP | `ask_mcp.py` is a legacy PostgreSQL loop; no protocol server | A thin **stdio** MCP adapter over the existing Tool Registry, READ allowlist only (section 5). `ask_mcp.py` is not the server and `/ask` is not routed into it. |

## 3. Community Intelligence (stored data)

### 3.1 Intake (unchanged)

Operator-provided captures only (topic, listing/search, user summary), as in
`community.md`. Genuine captures are supplied by HUMAN (T09). Parser changes
are limited to demonstrated real-capture differences (T10). Community data and
its command-log record commit in one transaction (T11).

### 3.2 Typed reads (T13)

Registry READ tools over stored Community tables: topic search/list, topic
detail with posts, author activity, coverage/work-queue status. Each result
carries forum host, topic id, post number, source URL, source timestamp,
capture timestamp, and coverage status (`pending`/`partial`/`complete`/
`coverage_unknown`). Absence in stored captures is reported as "not in stored
captures", never as nonexistence.

### 3.3 Classification and pain points (T14)

- Input: stored post text selected by id, bounded in length.
- Output: Pydantic-validated classification and pain-point records stored as
  **derived, versioned** rows: model identifier, prompt version, source post
  ids, character spans, source content hash, created time. Source rows are
  never modified.
- Reclassification creates a new version; a changed source hash marks older
  analyses stale.
- Inference is displayed as inference, with its supporting posts/spans.
- Post content is data: instructions in posts cannot select tools or actions.

### 3.4 Automated collection (conditional; not authorized)

Status: **AUTOMATED COLLECTION NOT AUTHORIZED / NOT IMPLEMENTED.**
It may be specified and implemented only after HUMAN records: the permitted
surfaces, source terms/access evidence, account conditions, retention and rate
limits, and explicit authorization. It would then be a separate fixed, typed
collector feeding the existing capture/discovery contracts — never generic
browsing. Operator captures may certify the Community E2E (Gate 3/9) but must
not be represented as an automated crawler.

## 4. Contact intelligence: candidates, evidence and CRM links (T17, T18)

- **Identity tiers:** exact unique evidence (e.g. same normalized email or
  domain-verified profile) vs. inference (name/username similarity). Only
  exact unique evidence may auto-match; everything else stays a candidate.
- **Promotion:** a candidate becomes canonical only through a saved proposal
  bound to the candidate id and version, reviewed and approved by the
  operator, applied atomically with a command-log id. Promotion preserves all
  evidence rows (source, URL, observed value, capture time, method).
- **Contact-method evidence:** canonical contact methods keep per-value
  provenance and verification state. "Verified" requires stored verification
  evidence; a model claim, timestamp, or appearance in a capture is not
  verification. Legacy scalar fields are not the only store of evidence.
- **Community ↔ CRM:** a discussion/post ↔ company/person link is a reviewed
  evidence association (source post, rationale, approver, command id),
  approved once, idempotent on replay. Usernames are never treated as
  verified identity.

## 5. Read-only MCP adapter (T15)

- Transport: **stdio** only (no new network listener).
- Surface: an explicit allowlist of registry tools with `risk = read`, using
  the same Pydantic argument schemas, handlers and result envelopes as `/ask`.
  Tool descriptors are generated from the registry so they cannot drift.
- Excluded: proposal, approval, apply, provider sync, draft write, send,
  arbitrary SQL, files, URLs, shell.
- Startup and reads perform no schema DDL. Any command-log/audit metadata a
  read writes is documented and contains counts/ids only.
- The adapter holds no provider credentials and starts no planner.
- LM Studio being an inference server does not prove it is an MCP client;
  Gate 4 requires a real MCP consumer to initialize/list/call.

## 6. Message content and the local model

Amends G2's invariant "message bodies never appear in ... prompts":

- Permitted: a selected, bounded excerpt (configured maximum characters) of a
  stored message or Community post may be sent to the **configured local
  model** (`LMSTUDIO_BASE_URL`, loopback/LAN host operated by the user) for
  classification or draft assistance.
- Forbidden: cloud/remote inference endpoints, credentials or tokens in any
  prompt, full mailbox dumps, bodies in ordinary logs or the command log,
  bodies in MCP tool descriptors.
- Model output is untrusted: validated, never executed, never treated as an
  instruction, never granted provider authority.

## 7. Local-LLM draft assistance (T27–T29)

Distinct, separately stored states, each with its own evidence:

1. **Suggestion** — model-generated text for a specific account/thread,
   stored with model id, prompt version and source message ids. Not a draft.
2. **Local draft version** — operator-accepted/edited text; immutable
   versions, bound to exactly one account and that account's thread.
3. **Provider draft** — explicit operator action mirrors a version to Gmail
   Drafts (`gmail.compose`); provider draft id recorded.
4. **Send approval** — bound to one immutable snapshot: account, version,
   recipients (To/Cc), subject, thread, attachments. Any edit invalidates it.
5. **Send attempt** — one claimed attempt with idempotency key;
   states `pending`/`sent`/`failed`/`unknown`. `unknown` (e.g. timeout)
   requires provider reconciliation, never a blind retry. A local key does not
   make a remote send exactly-once; reconciliation does.
6. **Sent** — only after provider readback confirms message id, thread and
   `SENT` label.

The planner, `/ask`, MCP and any model output cannot perform steps 3–6.

## 8. Email follow-up proposals (T29)

A follow-up marker on a stored message may produce a **task proposal**
(target company/person, due date resolved in `APP_TIMEZONE`, source
account/thread/message). Uncertain dates or targets require clarification.
Approval through the existing write-proposal path creates exactly one task with
source and command-log reference; a rejected proposal creates none; replay
creates no duplicate.

## 9. Cross-cutting rules

- **Account keys:** normalized account email is a required, immutable key on
  every mailbox object, cursor, draft, approval, attempt, task source and
  command-log argument; multi-account reads label every record; every mutating
  target is exactly one account (G2).
- **Provenance:** every extracted, discovered, classified or linked fact keeps
  source, source time vs. capture time, method, and command id.
- **Failure states:** planning, validation, execution and formatting failures
  are reported distinctly; failed tool envelopes and exceptions leave truthful
  terminal command states; failed/partial ingestion is counted separately from
  success and never hidden behind an advanced cursor.
- **No model provider authority:** models plan reads, extract, classify and
  suggest text. They never hold Gmail, Community, network, SQL, shell or file
  capability, and never approve.

Excluded (unchanged): PostgreSQL cutover, Calendar (C0/C1), campaigns,
generic web search/scraping, browser automation, Community posting/replies,
autonomous research or sending, SaaS/multi-tenancy, a new agent framework.

## 10. Trace to tasks and gates

| Workflow stage | Spec section | Tasks | Gate |
|---|---|---|---|
| Planner → registry → stored answer | 9 | T04–T05 | 1 |
| Integration of main + contact + Community | 1 | T06–T08 | 2 |
| A: operator capture → parse → persist | 3.1 | T09–T12 | 3 |
| A: automated collection | 3.4 | conditional (not authorized) | recorded status at 10 |
| A: classification → pain points → retrieval | 3.2–3.3 | T13–T14, T16 | 4 |
| A: MCP | 5 | T15–T16 | 4 |
| B: company/person/contact evidence, promotion | 4 | T17, T19 | 5 |
| A/B: Community ↔ CRM | 4 | T18–T19 | 5 |
| C: full/history sync, recovery, MIME | G2, 9 | T20–T24 | 6 |
| C: four accounts | G2, 9 | T25–T26 | 7 |
| C: suggestion → draft → approval → send → reconcile | 6–7 | T27–T28, T30 | 8 |
| C: follow-up proposal → task | 8 | T29–T30 | 8 |
| All workflows on one SHA | — | T31 | 9 |
| Restart/restore/operations | — | T32 | 10 |

HUMAN acknowledgement record (to be completed by the operator):

```text
Scope acknowledged: [ ] yes   Date: ____   By: ____
Automated Community collection: NOT AUTHORIZED (unchanged) / other: ____
```
