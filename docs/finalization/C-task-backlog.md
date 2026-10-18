# C. Implementation-ready task backlog

All paths below are relative to `C:\Users\verman\Desktop\LM STUDIO` or the future isolated integration checkout. Branch provenance is in A. `NEW` means a proposed file, not an existing module. Owners are primary accountable executors; a required local gate is explicitly owned by GROK_LOCAL. These are future tasks, not authorization to execute them during planning.

Global forbidden changes for every task: no real-data automated tests, destructive migration, unrelated refactor, PostgreSQL cutover, unrestricted model tools, generic crawler, autonomous email send, production credentials in cloud, or unrequested external action. Every implementation task requires focused tests followed by the full isolated suite, updated contracts where affected, a reviewed narrow diff and evidence on its exact commit. If a gate fails, stop downstream work and repair the same scope; never label it passed because HTTP returned 200. Additional task-specific restrictions and merge gates follow.

## M0 — preservation, baseline and specification

### T01 — Freeze evidence and preserve local work

- **PRIORITY:** P0. **OWNER:** GROK_LOCAL. **CURRENT STATE:** UNVERIFIED.
- **DEPENDENCIES:** None.
- **SCOPE:** Record executable baseline and preserve data/worktree evidence before any edit.
- **EXACT FILES OR MODULES:** `llm.py`, `services/llm_planner.py`, `tests/test_ask_llm_planner.py`; both registered worktrees; `leads.db` and any active WAL/SHM; Knowledge originals directory; reports named `Pasted markdown(4).md` and `(5).md` if available.
- **REQUIRED CHANGES:** Produce an evidence manifest with refs, tracked/untracked/dirty inventory, patch hashes, interpreter and service identity. Make an operator-approved consistent SQLite backup using SQLite backup semantics or a quiesced copy, plus Knowledge originals and protected local configuration backup. Reconcile available full-audit findings against this backlog without inventing unseen contents.
- **FORBIDDEN CHANGES:** No reset/stash/branch switch, secret dumps, mailbox access, schema init or user-data repair.
- **ACCEPTANCE CRITERIA:** Dirty patch recoverable; backup opens independently; counts/integrity measured on the copy; unavailable reports and Community permission failure recorded explicitly.
- **REQUIRED TESTS:** Backup integrity and restore rehearsal on a disposable path.
- **REQUIRED EVIDENCE:** Sanitized manifest, backup location/hash/time, integrity result, baseline counts and report-coverage table.
- **STOP CONDITIONS:** Inconsistent backup, unknown database identity, inaccessible worktree with unpreserved edits.
- **COMMIT / MERGE GATE:** Evidence only; no code commit or merge.

### T02 — Establish a safe Python 3.11+ test baseline

- **PRIORITY:** P0. **OWNER:** CLAUDE_CLOUD. **CURRENT STATE:** PARTIAL.
- **DEPENDENCIES:** T01 manifest.
- **SCOPE:** Reproducible supported runtime and test isolation before executing suites.
- **EXACT FILES OR MODULES:** `requirements.txt`, `README.md`, `.env.example`, `tests/test_query_counts.py`, `tests/pg_support.py`; NEW `tests/conftest.py` and `.github/workflows/tests.yml` only where no equivalent exists.
- **REQUIRED CHANGES:** Declare Python 3.11+; preserve main's `7c8f47b` isolation fix; eliminate default-db paths/provider credentials in tests. Fail tests if they try production DB/provider access. Document isolated environment commands; do not backport to Python 3.10. Resolve only actual supported-runtime failures. Remove the misleading active PostgreSQL example assignment or clearly keep it commented for default SQLite startup.
- **FORBIDDEN CHANGES:** No broad dependency churn or suppressing failing tests.
- **ACCEPTANCE CRITERIA:** Supported interpreter identified; collection and suite use temporary stores and fakes; baseline failures classified by branch and cause.
- **REQUIRED TESTS:** Isolation guard itself; current contact suite and main suite in separate disposable checkouts.
- **REQUIRED EVIDENCE:** Python/package versions, commands, counts/pass/fail/skip reasons, isolation proof.
- **STOP CONDITIONS:** Real credentials/database reachable; unsupported runtime; unexplained baseline failure.
- **COMMIT / MERGE GATE:** Separate environment/isolation correction; no feature integration yet.

### T03 — Ratify the narrow SQLite completion specification

- **PRIORITY:** P0. **OWNER:** CLAUDE_CLOUD. **CURRENT STATE:** PARTIAL.
- **DEPENDENCIES:** T01; may proceed alongside T02.
- **SCOPE:** Resolve source/spec mismatches and record this mission's authorized target without rewriting architecture.
- **EXACT FILES OR MODULES:** `AGENTS.md`, six required `docs/*.md` specifications, branch `docs/community.md`, `docs/knowledge.md`, `.env.example`.
- **REQUIRED CHANGES:** Carry forward main's G2 specification. Define CI stored-data reads/classification/CRM links and read-only MCP independently of automated collection; define governed candidate promotion, local LLM draft assistance and email-task proposals. Resolve G2's blanket prohibition of message bodies in prompts by explicitly permitting only selected bounded excerpts to the configured local model for classification/draft assistance; never credentials, cloud inference or ordinary log dumps. Keep SQLite supported, PostgreSQL unaccepted, existing phases independently gated. Specify account keys, provenance, approval snapshots, failure states and no model provider authority.
- **FORBIDDEN CHANGES:** No widening to Calendar, campaigns, generic search/scraping or automatic send.
- **ACCEPTANCE CRITERIA:** No remaining blanket prohibition conflicts with an intended stored-data feature; collection permission remains separate; every E2E maps to tasks and gates.
- **REQUIRED TESTS:** Contract/backlog trace review; no code tests for documentation alone.
- **REQUIRED EVIDENCE:** Before/after policy table, HUMAN acknowledgement of scope and conditional collection status.
- **STOP CONDITIONS:** A requested capability still has contradictory authority rules.
- **COMMIT / MERGE GATE:** Documentation-only prerequisite; not approval for future live actions.

## M1 — local planner E2E

### T04 — Complete the smallest planner correction

- **PRIORITY:** P0. **OWNER:** CLAUDE_CLOUD. **CURRENT STATE:** BROKEN.
- **DEPENDENCIES:** T02 and T01's preserved dirty diff.
- **SCOPE:** Correct the active typed local planner, not legacy `ask_mcp.py`.
- **EXACT FILES OR MODULES:** `llm.py`, `services/llm_planner.py`, `services/planner_validation.py`, `services/command_service.py`, `ask_router.py`, `config.py`, `.env.example`, `tests/test_ask_llm_planner.py`, `tests/test_ask_planner_validation.py`, `tests/test_command_log.py`.
- **REQUIRED CHANGES:** Review/adopt only justified parts of the dirty patch. Add planner-specific configurable timeout/output budget and compact argument schemas to the catalog. Treat reasoning-off as a provider option requiring local proof; accept final content only. Distinguish timeout, empty/truncated content, malformed JSON, invalid arguments and handler error. Ensure failed tool envelopes/exceptions and clarification leave truthful terminal/awaiting states. Persist safe counts/IDs/status metadata in read-command summaries rather than full private result records; keep returning authorized records to the caller. Preserve immutable write-proposal payloads separately from ordinary diagnostic logging. Preserve deterministic fallback with explicit planner-failure metadata.
- **FORBIDDEN CHANGES:** No parsing reasoning as commands, unbounded retries, global timeout inflation, model-generated SQL or routing rewrite.
- **ACCEPTANCE CRITERIA:** Valid plans validate once; invalid plans execute nothing; timeout is bounded and disclosed; handler errors cannot be reported as successful execution.
- **REQUIRED TESTS:** Reasoning-only, invalid/empty/truncated output, timeout, unknown tool/fields, tool error envelope/exception, clarification audit, deterministic bypass, count accuracy and private-result log redaction.
- **REQUIRED EVIDENCE:** Narrow diff, sanitized fake request/response fixtures, exact test results.
- **STOP CONDITIONS:** Provider behavior must be guessed or a fix requires changing unrelated inference paths.
- **COMMIT / MERGE GATE:** Candidate fix only; T05 required before declaring planner fixed.

### T05 — Certify Qwen → registry → stored-data answer

- **PRIORITY:** P0. **OWNER:** GROK_LOCAL. **CURRENT STATE:** UNVERIFIED.
- **DEPENDENCIES:** T04.
- **SCOPE:** Gate 1 against local LM Studio and known stored-data snapshot.
- **EXACT FILES OR MODULES:** T04 path; local model configuration; temporary backup database; `/ask` and command-log evidence.
- **REQUIRED CHANGES:** None expected. Use a local isolated application instance and the real model; capture model identifier, endpoint, request options, timings, validated tool/arguments and returned data. Run three useful paraphrases chosen to bypass deterministic routing plus one deterministic control and one unsafe request.
- **FORBIDDEN CHANGES:** No production `/ask` audit writes, no Gmail calls, no treating direct inference as planner proof.
- **ACCEPTANCE CRITERIA:** All three real planner requests complete within the recorded configured bound, select expected tools/filters and match independent copy-based query results. Unsafe selection executes no write; deterministic control bypasses planning. Record observed latency without claiming an unmeasured SLA.
- **REQUIRED TESTS:** T04 regression plus real inference E2E and failure fallback.
- **REQUIRED EVIDENCE:** Exact commit/patch hash, model settings, sanitized traces, command IDs, records/counts/filter comparison, timings.
- **STOP CONDITIONS:** Reasoning setting ignored, no final JSON, repeated timeout, wrong tool/filters or silent fallback.
- **COMMIT / MERGE GATE:** Gate 1 PASS enables integration; failure returns to T04.

## M2 — staged integration

### T06 — Integrate main and contact intelligence in isolation

- **PRIORITY:** P0. **OWNER:** CLAUDE_CLOUD. **CURRENT STATE:** PARTIAL.
- **DEPENDENCIES:** T03, T05.
- **SCOPE:** Preserve both branches' functionality on a separate integration branch.
- **EXACT FILES OR MODULES:** B's conflict list; `knowledge/*`, `tools/registry.py`, contact/research modules, Gmail modules and tests; planner patch separately.
- **REQUIRED CHANGES:** Verify refs, branch from main, merge the contact history and resolve overlaps by behavior. Retain Knowledge tool registration, new Gmail/CRM behavior, response metadata, config, routes/navigation and isolation fixes. Carry certified planner patch independently. Record G2-0 decision as integrate existing Gmail work.
- **FORBIDDEN CHANGES:** No merge in dirty primary checkout, whole-file conflict resolution that drops features, squashing away unreviewed work or duplicate Gmail implementation.
- **ACCEPTANCE CRITERIA:** Registry and route inventories include both baselines; no lost schema/fields; both preexisting suites pass on integrated code.
- **REQUIRED TESTS:** Planner, contacts/research/candidates, write governance, Gmail full sync/linking, Knowledge ingestion/K1/search, route/response counts; full isolated suite.
- **REQUIRED EVIDENCE:** Parent SHAs, conflict resolution record, before/after inventories and test report.
- **STOP CONDITIONS:** Ref changed, baseline unclear, conflict requires unrelated redesign.
- **COMMIT / MERGE GATE:** Reviewed integration commit on isolated branch only; main promotion awaits T08.

### T07 — Integrate committed Community implementation

- **PRIORITY:** P0. **OWNER:** CLAUDE_CLOUD. **CURRENT STATE:** PARTIAL.
- **DEPENDENCIES:** T06; T01 resolves preservation of the separate local worktree.
- **SCOPE:** Reuse committed Community tip `5acee41` and its three-commit history.
- **EXACT FILES OR MODULES:** `community_db.py`, `community_parser.py`, `community_discovery.py`, `community_schemas.py`, `services/community_*`, Community templates/tests, `app.py`, `db.py`, `AGENTS.md`, Community/roadmap/product docs and shared navigation.
- **REQUIRED CHANGES:** Merge committed Community work into the isolated integration branch. Preserve T03's scope wording, all source identities, capture formats, revision history and derived work queue. Leave automatic fetching absent.
- **FORBIDDEN CHANGES:** No assuming stale local `86bb246` equals remote tip; no replacing Gmail or Knowledge initialization/navigation.
- **ACCEPTANCE CRITERIA:** Topic, listing/search and user-summary synthetic tests pass; existing routes/registry remain intact; source format limits remain visible.
- **REQUIRED TESTS:** `test_community.py`, `test_community_discovery.py`, full integrated suite.
- **REQUIRED EVIDENCE:** Merge parents, conflicts, fixture status, route inventory and test outputs.
- **STOP CONDITIONS:** Unpreserved Community changes, incompatible shared routes or discarded safety rules.
- **COMMIT / MERGE GATE:** Isolated integration commit; no claim of real Community compatibility.

### T08 — Certify migration order and integrated regressions

- **PRIORITY:** P0. **OWNER:** GROK_LOCAL. **CURRENT STATE:** UNVERIFIED.
- **DEPENDENCIES:** T07.
- **SCOPE:** Gate 2 and a minimal explicit SQLite schema boundary for completion work.
- **EXACT FILES OR MODULES:** `db.py`, `gmail_db.py`, `community_db.py`, `knowledge/repository.py`, `repositories/sqlite_store.py`, application startup; NEW `scripts/migrate_sqlite.py` and `migrations/sqlite/` for subsequent additions; `tests/test_routes.py` and migration tests.
- **REQUIRED CHANGES:** Rehearse integrated startup/initialization on an empty database and backup copy. Inventory all DDL and `executescript` calls; establish explicit versioned SQLite migrations before new schema work, with no import/read-time DDL. If correction is needed, hand a bounded defect to CLAUDE_CLOUD and verify its revision. Document rollback via reversal where safe or verified restore when data would be lost.
- **FORBIDDEN CHANGES:** No real DB initialization, destructive table rebuild, Alembic history rewrite or claiming PostgreSQL accepted.
- **ACCEPTANCE CRITERIA:** Two upgrades have identical schema/data results; legacy counts/IDs/hashes preserved; rollback/restore works; integrated suite and copy-based local routes pass.
- **REQUIRED TESTS:** Empty/legacy/copy upgrades, repeat/restart, failure rollback, foreign-key/integrity checks and full suite.
- **REQUIRED EVIDENCE:** Schema versions/order, reconciliation report, PASS/FAIL/SKIP with reasons and certified SHA.
- **STOP CONDITIONS:** Data mismatch, unexpected DDL transaction boundary, accidental real DB/provider reachability.
- **COMMIT / MERGE GATE:** Gate 2 PASS permits reviewed integration promotion; engineering fixes committed by CLAUDE_CLOUD only after re-verification.

## M3 — genuine Community ingestion

### T09 — Supply permitted genuine Community captures

- **PRIORITY:** P0. **OWNER:** HUMAN. **CURRENT STATE:** MISSING.
- **DEPENDENCIES:** T08, T03.
- **SCOPE:** Genuine evidence for the operator-capture path; separate automated-access decision.
- **EXACT FILES OR MODULES:** Local private capture bundle; `docs/community.md`; `tests/fixtures/community/README.md`.
- **REQUIRED CHANGES:** Save through permitted manual access one listing/search capture, the relevant user summary and at least one real topic with reply/timestamp evidence. Include source URL, capture time, method, declared post count and known visibility limits. Supply a later/overlapping capture if available for revisions/replay. Record whether automated access is permitted, prohibited or unresolved with evidence; unresolved selects manual capture.
- **FORBIDDEN CHANGES:** No crawler/API harvesting/browser automation or committing private captures by default.
- **ACCEPTANCE CRITERIA:** Authentic captures inspectable locally; expected fields/counts manually annotated; payload shape known. Any unsupported actual platform shape is reported, not relabeled Discourse.
- **REQUIRED TESTS:** Manual source/capture comparison.
- **REQUIRED EVIDENCE:** Private fixture manifest with hashes, URLs, times, expected records and permission status.
- **STOP CONDITIONS:** Access not permitted or provenance cannot be established; parser work waits for genuine material.
- **COMMIT / MERGE GATE:** No source commit; sanitized fixture inclusion requires permission.

### T10 — Correct only demonstrated real-capture parser gaps

- **PRIORITY:** P0. **OWNER:** CLAUDE_CLOUD. **CURRENT STATE:** UNVERIFIED.
- **DEPENDENCIES:** T09's permitted/sanitized fixture contract.
- **SCOPE:** Compatibility of existing topic/discovery/summary parsers with actual supplied content.
- **EXACT FILES OR MODULES:** `community_parser.py`, `community_discovery.py`, `community_schemas.py`, `tests/test_community.py`, `tests/test_community_discovery.py`, fixtures README.
- **REQUIRED CHANGES:** Add representative permitted fixtures or structurally faithful redacted cases with private originals retained locally. Correct proven field/identity/date/body/URL differences; expose unsupported formats and incomplete capture status. Do not assume user-summary topic count is 13.
- **FORBIDDEN CHANGES:** No speculative parser families, fetched samples or invented author/profile facts.
- **ACCEPTANCE CRITERIA:** Expected topics/posts/replies/timestamps/errors match annotated capture; unknown fields do not become facts; all prior synthetic cases remain valid.
- **REQUIRED TESTS:** Real-shape cases, missing timestamps, URL-ID conflict, duplicate IDs, partial stream, HTML safety and malformed payload.
- **REQUIRED EVIDENCE:** Field comparison, fixture origin/redaction note and targeted/full tests.
- **STOP CONDITIONS:** Source platform materially differs; document exact mismatch and amend only this capture adapter before proceeding.
- **COMMIT / MERGE GATE:** Small compatibility commit; real local proof deferred to T12.

### T11 — Make Community data and audit atomic

- **PRIORITY:** P0. **OWNER:** CLAUDE_CLOUD. **CURRENT STATE:** PARTIAL.
- **DEPENDENCIES:** T10, T08 migration boundary.
- **SCOPE:** Trustworthy ingest/discovery receipts and nonmutating reads.
- **EXACT FILES OR MODULES:** `services/community_ingest_service.py`, `services/community_discovery_service.py`, `community_db.py`, `repositories/command_log_store.py`, `repositories/sqlite_store.py`, Community tests.
- **REQUIRED CHANGES:** Move operational schema setup outside data/read handlers. Reuse active SQLite transaction propagation so capture/discovery data and required counts-only command receipt commit together. Define duplicate replay receipt semantics without duplicating domain evidence. Check migrations without changing schema during reads.
- **FORBIDDEN CHANGES:** No best-effort audit presented as guaranteed, `executescript` within an owned write transaction, broad repository rewrite or deletion of absent posts.
- **ACCEPTANCE CRITERIA:** Audit failure rolls back the command's data effects; success always has a committed command ID; repeat capture changes no domain records; read paths issue no DDL.
- **REQUIRED TESTS:** Inject audit/source/post/checkpoint failure, repeat capture, restart, read-only connection and revision retention.
- **REQUIRED EVIDENCE:** Transaction trace, before/after data counts and receipt IDs, full suite.
- **STOP CONDITIONS:** Shared transaction cannot be propagated narrowly; document smallest boundary before editing further.
- **COMMIT / MERGE GATE:** Dedicated atomicity commit before real persistence certification.

### T12 — Prove genuine parsing and persistence locally

- **PRIORITY:** P0. **OWNER:** GROK_LOCAL. **CURRENT STATE:** UNVERIFIED.
- **DEPENDENCIES:** T11.
- **SCOPE:** Gate 3, with private original captures and a disposable database.
- **EXACT FILES OR MODULES:** `/community/discover`, `/community/ingest`, `community_*` tables, private T09 evidence.
- **REQUIRED CHANGES:** None unless a defect is returned to T10/T11. Ingest, replay, add overlap/update capture and restart; inspect persisted evidence using read-only copy-based queries.
- **FORBIDDEN CHANGES:** No scrape or production data writes; no calling synthetic results real compatibility.
- **ACCEPTANCE CRITERIA:** Exact expected records, source URLs, authors, timestamps and revisions; partial/unknown coverage remains visible; replay has zero duplicate domain records and restart preserves queue/checkpoints.
- **REQUIRED TESTS:** Local route/storage E2E plus failure-injection suite from T11.
- **REQUIRED EVIDENCE:** Capture hashes, annotations versus DB comparison, command IDs, replay delta and restart result.
- **STOP CONDITIONS:** Missing provenance, wrong identities/counts, undisclosed partial coverage or audit mismatch.
- **COMMIT / MERGE GATE:** Gate 3 PASS; evidence note only, parser fixes return to cloud.

## M4 — grounded Community retrieval and MCP

### T13 — Add typed stored-Community read tools

- **PRIORITY:** P0. **OWNER:** CLAUDE_CLOUD. **CURRENT STATE:** MISSING.
- **DEPENDENCIES:** T12 and T03.
- **SCOPE:** Search and retrieve existing Community evidence through accepted services.
- **EXACT FILES OR MODULES:** `community_schemas.py`, `community_db.py`, `repositories/sqlite_store.py`, `tools/registry.py`, `tools/read_handlers.py`, `ask_router.py`, `response_presentation.py`; NEW `services/community_query_service.py`, `tests/test_community_tools.py`.
- **REQUIRED CHANGES:** Register READ tools `search_community` and `get_community_topic` with bounded text/host/topic/author/date/coverage/pagination filters. Reuse storage identities and result envelope. Return capture/post IDs, URLs, capture/source dates, coverage and matching snippets; preserve PostgreSQL fail-closed behavior.
- **FORBIDDEN CHANGES:** No arbitrary query language, schema initialization or fetching in search; no replacing source storage with Knowledge copies.
- **ACCEPTANCE CRITERIA:** `/ask` routes to the tools; filters/counts/offset/provenance match deterministic queries; unknown fields rejected; no-match does not imply absence from the Community.
- **REQUIRED TESTS:** Filter/pagination/counts, ambiguity, unsafe arguments, no network/write, unsupported backend and presentation.
- **REQUIRED EVIDENCE:** Schemas, registry inventory, expected result fixtures, targeted/full tests.
- **STOP CONDITIONS:** A result cannot identify its source evidence or reads mutate schema.
- **COMMIT / MERGE GATE:** Narrow read-only feature commit; no MCP capability claim yet.

### T14 — Persist versioned classification and pain-point evidence

- **PRIORITY:** P1. **OWNER:** CLAUDE_CLOUD. **CURRENT STATE:** MISSING.
- **DEPENDENCIES:** T13, T08.
- **SCOPE:** Local advisory analysis of stored topic/post evidence.
- **EXACT FILES OR MODULES:** `community_schemas.py`, `community_db.py`, T13 service/read schemas; NEW `services/community_analysis_service.py`, versioned SQLite analysis migration, `tests/test_community_analysis.py`; reuse patterns from `knowledge/classification.py` and email classification.
- **REQUIRED CHANGES:** Define bounded taxonomy/summary/pain-point models with evidence post IDs and exact supporting spans, confidence, model/prompt version and input hash. Run inference outside write transaction; validate references/spans and then persist through one typed service. Invalidate/mark stale when source revision changes. Expose analysis filters and separate source fact from inference.
- **FORBIDDEN CHANGES:** No inferred verified identity/contact, model writes, fabricated quotes or deleting raw evidence on model failure.
- **ACCEPTANCE CRITERIA:** Analysis replay reuses same source/version result; invalid citations rejected; model outage preserves intake; changed source marks old analysis stale.
- **REQUIRED TESTS:** Prompt injection, malformed output, nonexistent post/span, unavailable model, idempotence, revision invalidation and retrieval filters.
- **REQUIRED EVIDENCE:** Typed contract, migration/reversal proof, classified fixture with evidence references, full tests.
- **STOP CONDITIONS:** Analysis cannot be grounded in supplied posts or requires unrestricted content/network access.
- **COMMIT / MERGE GATE:** Separate advisory-analysis commit; quality and real inference checked in T16.

### T15 — Implement a thin read-only MCP adapter

- **PRIORITY:** P0. **OWNER:** CLAUDE_CLOUD. **CURRENT STATE:** MISSING.
- **DEPENDENCIES:** T14; technically T13 plus integrated registry.
- **SCOPE:** Standards-compatible stdio transport over existing stored-data reads.
- **EXACT FILES OR MODULES:** `tools/registry.py`, `tools/envelope.py`, read handlers/repositories, `requirements.txt`; NEW `mcp_server.py`, `tests/test_mcp_server.py`, `docs/mcp.md`; leave `ask_mcp.py` inactive.
- **REQUIRED CHANGES:** Use a maintained MCP SDK with a documented tested version. Derive descriptors from registry input schemas, export an explicit READ allowlist (contacts/analytics/follow-ups, imported email reads, Knowledge search and Community tools). Revalidate risk/name/args on every call; validate ToolResult and translate its error status into MCP error semantics. No runtime schema setup. Make the server use an existing initialized DB and fail closed if migration is needed.
- **FORBIDDEN CHANGES:** No new planner/framework, remote HTTP server, arbitrary SQL/file/resource exposure, provider credentials, sync, candidate/proposal/apply/send tools. Do not wrap legacy `ask_mcp.py`.
- **ACCEPTANCE CRITERIA:** Real MCP initialize/list/call works; every advertised tool is callable; forbidden names rejected even if later registered; descriptors match app schemas. Domain tables remain unchanged by reads. Any command-log audit writes are explicitly documented metadata-only; no bodies/private result dumps. Do not claim byte-for-byte DB immutability if audit metadata is written.
- **REQUIRED TESTS:** Protocol smoke with SDK client, descriptor parity, risk bypass, malicious args, handler errors, no provider/network/domain writes and startup without migrations.
- **REQUIRED EVIDENCE:** Allowlist, transport/version, SDK client transcript, tests.
- **STOP CONDITIONS:** A handler requires schema mutation/provider access or client compatibility is unproven.
- **COMMIT / MERGE GATE:** Transport-only commit; real local consumer proof T16.

### T16 — Certify classified retrieval and MCP with local data/model

- **PRIORITY:** P0. **OWNER:** GROK_LOCAL. **CURRENT STATE:** UNVERIFIED.
- **DEPENDENCIES:** T15.
- **SCOPE:** Gate 4 using genuine stored captures and a real MCP-capable client.
- **EXACT FILES OR MODULES:** T13–T15 paths, `/ask`, local LM Studio and configured MCP client.
- **REQUIRED CHANGES:** Configure local stdio launch against a disposable initialized dataset. Select the actual client explicitly; verify whether installed LM Studio consumes MCP before claiming that path. Otherwise use an SDK/compatible MCP client and independently verify LM Studio through `/ask`.
- **FORBIDDEN CHANGES:** No claiming model inference server equals MCP consumer; no exposing extra tools to make a demo work.
- **ACCEPTANCE CRITERIA:** Real local classification has traceable pain-point evidence; a genuine Community question via `/ask` and matching MCP call return the same IDs/counts/provenance; unsafe calls denied; model outage returns evidence/failure truthfully.
- **REQUIRED TESTS:** Real tool enumeration/call, source spot-check, injection attempt, unavailable-model and read-effect comparison.
- **REQUIRED EVIDENCE:** Client/model/version/settings, protocol trace, tool/args/result IDs, source comparison and denied call evidence.
- **STOP CONDITIONS:** Local model never selects tool, unsupported client configuration or inference displayed as verified source fact.
- **COMMIT / MERGE GATE:** Gate 4 PASS with any client limitation recorded explicitly.

## M5 — contact and Community association

### T17 — Finish governed candidate promotion and canonical contact evidence

- **PRIORITY:** P0. **OWNER:** CLAUDE_CLOUD. **CURRENT STATE:** PARTIAL.
- **DEPENDENCIES:** T16, T08.
- **SCOPE:** Complete company/person discovery-to-CRM using existing intake/research/candidate infrastructure.
- **EXACT FILES OR MODULES:** `candidate_models.py`, `db.py`, `repositories/sqlite_store.py`, `services/research_job_runner.py`, `services/discovery_outcome_materialization.py`, `services/write_proposal_authority.py`, `services/write_proposals.py`, operator routes/templates; NEW `services/candidate_review_service.py`, `tests/test_candidate_promotion.py`, versioned SQLite contact-evidence migration.
- **REQUIRED CHANGES:** Reuse exact identity matching and bounded website/manual-source discovery. Add version-bound preview/approval/apply for person/contact candidates, preserving source IDs, normalized value, confidence, verification basis and history. Add only the missing SQLite canonical contact-method evidence representation; legacy scalar fields remain compatible projections. Unknown legacy provenance stays unknown/unverified. Require concrete verification evidence, not only a timestamp. Connect approved output to research-job receipt and contact reads.
- **FORBIDDEN CHANGES:** No generic people-search provider, automatic merge, model-inferred verified state, bulk canonical overwrite or replacing leads with a new CRM schema.
- **ACCEPTANCE CRITERIA:** Same approved candidate applies once atomically; conflicting owners/values stay in review; person/company source chain and multiple methods remain queryable; legacy reads continue working.
- **REQUIRED TESTS:** Stale approval, duplicate/replay, transaction failure, owner conflict, inferred verification rejection, legacy projection, research-job recovery and truthful coverage metrics.
- **REQUIRED EVIDENCE:** Exact schema delta, approval diff, applied IDs/command ID/source chain and tests.
- **STOP CONDITIONS:** Identity needs guessing, no supporting evidence or unrelated model redesign appears necessary.
- **COMMIT / MERGE GATE:** Bounded promotion/evidence commits; Gate 5 local proof required.

### T18 — Link Community discussions to CRM through reviewed evidence

- **PRIORITY:** P1. **OWNER:** CLAUDE_CLOUD. **CURRENT STATE:** MISSING.
- **DEPENDENCIES:** T17 and T14.
- **SCOPE:** Traceable company/person/topic/post association and uncertainty review.
- **EXACT FILES OR MODULES:** `community_db.py`, `community_schemas.py`, candidate review service, `services/gmail_linking.py` matching patterns, lead/Community views; NEW `services/community_crm_linking.py`, `tests/test_community_crm_links.py`, versioned link migration.
- **REQUIRED CHANGES:** Store proposed/approved/rejected/stale evidence links with topic/post/capture, lead/person, basis/confidence and approval version. Exact uniquely supported identities may be proposed deterministically; usernames or name similarity alone never prove person/company identity. Show linked discussions, pain points, existing email communications and research tasks on CRM detail; preserve evidence on rejection/relink.
- **FORBIDDEN CHANGES:** No automatic canonical identity promotion, thread authors becoming contacts without approval, source overwrites or outreach generation from a match alone.
- **ACCEPTANCE CRITERIA:** Approved link navigates both directions to source evidence; ambiguity remains visible; source edits stale affected inferences; repeat approval adds no duplicate.
- **REQUIRED TESTS:** Exact/ambiguous/no-match identities, duplicate username across hosts, stale source, approval/rejection/replay and navigation queries.
- **REQUIRED EVIDENCE:** Example proposed versus committed link, source chain, account-visible communication labels and tests.
- **STOP CONDITIONS:** Link lacks evidence or combines conflicting people/companies.
- **COMMIT / MERGE GATE:** Association-only commit, no new external discovery.

### T19 — Certify the complete company/contact workflow

- **PRIORITY:** P0. **OWNER:** GROK_LOCAL. **CURRENT STATE:** UNVERIFIED.
- **DEPENDENCIES:** T18.
- **SCOPE:** Gate 5 and E2E B on a local disposable copy.
- **EXACT FILES OR MODULES:** Intake, candidate/research services, approved CRM writes, Community links, stored-email association and follow-up reads.
- **REQUIRED CHANGES:** Demonstrate one permitted company discovery → identity → person/contact evidence → dedup → approval → CRM record → discussion/email association → research task/follow-up chain. Use existing stored communication; live Gmail is not required here.
- **FORBIDDEN CHANGES:** No invented live provider verification, unauthorized website research, silently collapsing two identities or production writes.
- **ACCEPTANCE CRITERIA:** Entire chain traceable; replay creates no duplicate canonical entity/task; ambiguous counterpart remains unverified; existing contacts retain prior values/provenance.
- **REQUIRED TESTS:** Local operator review/apply, replay, injected rollback, ambiguity and grounded `/ask` retrieval.
- **REQUIRED EVIDENCE:** Sanitized entity/source/candidate/job/command IDs, expected versus actual facts and delta counts.
- **STOP CONDITIONS:** Unsupported identity conclusion, lost provenance or missing approval.
- **COMMIT / MERGE GATE:** Gate 5 PASS; defects return to T17/T18.

## M6 — reliable Gmail ingestion

### T20 — Diagnose the reported 113 Gmail failures locally

- **PRIORITY:** P0. **OWNER:** GROK_LOCAL. **CURRENT STATE:** UNVERIFIED.
- **DEPENDENCIES:** T08; can precede T19 without editing its files.
- **SCOPE:** Determine what the reported failure number actually measures.
- **EXACT FILES OR MODULES:** Backup-copy `gmail_sync_state`, `gmail_mailbox_sync_state`, `command_log`, stored message/source tables; locally available sanitized logs; sync/normalizer source.
- **REQUIRED CHANGES:** Read copy only. Separate cumulative attempts from distinct failed messages, provider failures, decode/validation, classification fallback, database failure and missing evidence. Compare count formulas to observed state. Produce reproducible sanitized fixtures for known causes. List any message identities not recoverable from existing logs; do not invent a 113-ID list.
- **FORBIDDEN CHANGES:** No OAuth/token loading, Gmail provider calls, retry or real database changes.
- **ACCEPTANCE CRITERIA:** Each available failure is classified; limitations and possible overcount/gaps are explicit; the smallest correction/reconciliation procedure is specified.
- **REQUIRED TESTS:** Offline reproduction for each evidenced failure class.
- **REQUIRED EVIDENCE:** Failure taxonomy, distinct-versus-attempt counts, redacted error codes, fixture hashes and remaining unknowns.
- **STOP CONDITIONS:** Live access is needed to identify missing records; defer that portion to authorized T24.
- **COMMIT / MERGE GATE:** Evidence only; findings feed T21–T23.

### T21 — Make full synchronization recoverable and correctly counted

- **PRIORITY:** P0. **OWNER:** CLAUDE_CLOUD. **CURRENT STATE:** PARTIAL.
- **DEPENDENCIES:** T20, T19, T08.
- **SCOPE:** Preserve existing full-page intake and close item-loss/checkpoint/counter/audit gaps.
- **EXACT FILES OR MODULES:** `services/gmail_sync_service.py`, `gmail_db.py`, `gmail_schemas.py`, `providers/fake_gmail.py`, full-sync tests; NEW failed-item migration and `tests/test_gmail_failed_items.py`.
- **REQUIRED CHANGES:** Commit page results plus a durable account/message failed-item ledger before advancing cursor, or roll back the page. Use per-item savepoints so a partial failed item leaves no orphan. Record only per-page count deltas and distinguish attempts, unique failures and outstanding failures. Keep bounded resume-to-exhaustion; invalid page token recovery replays idempotently. Mark complete only with explicit outstanding-item state. Audit full sync and replay/recovery with correct receipts. Keep model classification failure independent of ingestion loss.
- **FORBIDDEN CHANGES:** No swallowing failures as completed, destructive restart, implicit bulk reclassification or infinite retry.
- **ACCEPTANCE CRITERIA:** Every listed ID is committed, durably retryable or explicitly unavailable; restart/replay loses no item; multi-page counters equal actual outcomes.
- **REQUIRED TESTS:** Mid-item/mid-page crash, audit failure, 2+ pages, invalid token, duplicate page, retry once/retry cap, malformed message and failed final page.
- **REQUIRED EVIDENCE:** Ledger/checkpoint/count invariants, migration rehearsal and full suite.
- **STOP CONDITIONS:** Cursor can advance past an unaccounted ID or transaction ownership is unclear.
- **COMMIT / MERGE GATE:** Recovery-first commit; no live sync certification yet.

### T22 — Add history synchronization and mailbox-state reconciliation

- **PRIORITY:** P0. **OWNER:** CLAUDE_CLOUD. **CURRENT STATE:** MISSING.
- **DEPENDENCIES:** T21.
- **SCOPE:** Full-to-incremental handoff with convergent provider state, account-ready keys from the start.
- **EXACT FILES OR MODULES:** `providers/gmail.py`, `providers/fake_gmail.py`, `services/gmail_sync_service.py`, `gmail_db.py`, `gmail_schemas.py`, integration status view; NEW `tests/test_gmail_history_sync.py` and mailbox-state migration.
- **REQUIRED CHANGES:** Capture profile history boundary before full scan and replay history afterward to close the moving-mailbox gap. Persist account-scoped history cursors after covered data/failures commit. Handle history expiry with a recorded bounded full rescan. Persist label/deleted/archive/spam/trash state and reconcile updates without losing immutable source history. Define full-mailbox scope explicitly, including G2 Spam/Trash convergence; ordinary views may filter them. Add bounded provider retry/backoff/error categories and per-account health.
- **FORBIDDEN CHANGES:** No assuming list counts prove completeness or deleting source evidence when Gmail deletes a message.
- **ACCEPTANCE CRITERIA:** Full plus history yields no gaps/duplicates; same history replay has no domain changes; 404 history falls back safely; labels/deletions converge with auditable tombstones.
- **REQUIRED TESTS:** New mail during scan, history pagination/replay, expired cursor, message deletion, labels/archive/spam/trash, 401/403/429/5xx, crash before cursor commit.
- **REQUIRED EVIDENCE:** Provider contracts, cursor diagram, fake traces and full suite.
- **STOP CONDITIONS:** Provider semantics not verified against current official API docs at implementation time; no guessing cursor guarantees.
- **COMMIT / MERGE GATE:** Incremental/reconciliation commit after T21 invariants pass.

### T23 — Complete message MIME, headers and bounded attachments

- **PRIORITY:** P1. **OWNER:** CLAUDE_CLOUD. **CURRENT STATE:** PARTIAL.
- **DEPENDENCIES:** T22; independent MIME fixes may be prepared after T20.
- **SCOPE:** G2 receive/display fidelity needed for correct replies and attachments.
- **EXACT FILES OR MODULES:** `providers/gmail_normalize.py`, `providers/gmail.py`, `gmail_schemas.py`, `gmail_db.py`, email templates, `services/email_classification_service.py`; NEW `tests/test_gmail_mime.py`, attachment/header migration.
- **REQUIRED CHANGES:** Persist queryable Reply-To, RFC IDs, In-Reply-To, References and labels; decode declared charsets with warnings. Support bounded on-demand attachment/inline retrieval and safe content-addressed storage. Sanitize HTML and never auto-fetch remote images. Keep original content, with quote/signature stripping only for display/model excerpts. Preserve existing deterministic and validated model classification with version/source evidence.
- **FORBIDDEN CHANGES:** No unsanitized HTML, automatic remote attachment fetch, invented MIME fields or logging bodies.
- **ACCEPTANCE CRITERIA:** Plain/HTML/alternative/mixed/related/non-UTF-8 cases are readable; malformed inputs degrade safely; attachment bytes/hash/size and header evidence survive reload.
- **REQUIRED TESTS:** All MIME variants, malformed/base64/charset cases, oversized attachments, unsafe HTML/links, no remote image request and classification outage/injection.
- **REQUIRED EVIDENCE:** Built-in-test MIME fixtures, fidelity/security comparisons and migration/full tests.
- **STOP CONDITIONS:** Content cannot be decoded without fabricated text or an unsafe display path is needed.
- **COMMIT / MERGE GATE:** MIME/attachment commit before live Gate 6.

### T24 — Certify full/history synchronization and historical recovery

- **PRIORITY:** P0. **OWNER:** GROK_LOCAL. **CURRENT STATE:** UNVERIFIED.
- **DEPENDENCIES:** T23; HUMAN authorization for named test mailbox and synchronization.
- **SCOPE:** Gate 6, first one disposable/test account, then explicitly permitted existing mailbox.
- **EXACT FILES OR MODULES:** Full/history sync, failed-item ledger, mailbox state, provider readback and local status UI.
- **REQUIRED CHANGES:** Execute the documented bounded procedure only after operator permission. Complete full scan/history catch-up, reconcile IDs/scope rather than aggregate profile count alone, recover or individually account for missing/failed records, interrupt/restart and replay. Compare model-assisted classification on a bounded real sample without making the model a sync prerequisite.
- **FORBIDDEN CHANGES:** No live Gmail access during planning or automated tests; no mass reset or blind retry.
- **ACCEPTANCE CRITERIA:** No unexplained missing/duplicate IDs in declared scope; zero unresolved recoverable failures; remaining deleted/inaccessible items explicitly evidenced and operator-accepted. Count 113 is explained or marked historically unreconstructable with a new complete reconciliation. Restart/history correctness demonstrated.
- **REQUIRED TESTS:** Real full/history/replay, controlled arrival/state change, restart; fake failure suite still green.
- **REQUIRED EVIDENCE:** Permission/scope, redacted account identity, counts/ID reconciliation, failed-item dispositions, cursor checkpoints and command IDs.
- **STOP CONDITIONS:** No explicit mailbox authorization, provider mismatch, unresolved loss or secrets/log leakage.
- **COMMIT / MERGE GATE:** Gate 6 PASS; evidence only, corrections return to cloud.

## M7 — four-account isolation

### T25 — Configure and enforce four account identities everywhere

- **PRIORITY:** P0. **OWNER:** CLAUDE_CLOUD. **CURRENT STATE:** PARTIAL.
- **DEPENDENCIES:** T24.
- **SCOPE:** Reuse existing account/message and account/thread keys; close configuration/query/projection gaps.
- **EXACT FILES OR MODULES:** `config.py`, `scripts/gmail_authorize.py`, `providers/gmail.py`, `services/gmail_sync_service.py`, `services/gmail_linking.py`, `gmail_db.py`, `tools/read_inputs.py`, `tools/read_handlers.py`, email routes/templates; NEW `tests/test_gmail_accounts.py`, versioned account migration.
- **REQUIRED CHANGES:** Up to four enabled accounts with distinct token paths, immutable normalized provider identity and local aliases. Validate token/profile identity against selected account. Convert global label-sync state to per-account; backfill only from proven stored account identity. Require account on thread/draft/write targets. Permit explicit all-account lists with account shown; no implicit mixed thread. Scope projections/health/errors/locks/retries and CRM communication views. Make account failure independent.
- **FORBIDDEN CHANGES:** No shared token file, guessed legacy account, SaaS user system or silent all-account writes.
- **ACCEPTANCE CRITERIA:** Same message/thread IDs in two accounts remain separate through DB, tools, MCP, UI and linking; revoke/disable one does not affect three others. Ambiguous migration stops for review.
- **REQUIRED TESTS:** Four fake accounts, duplicate IDs, cross-account route/tool lookups, aliases, wrong token identity, enable/disable, revoked token and legacy migration.
- **REQUIRED EVIDENCE:** Account propagation matrix, migration reconciliation and full suite.
- **STOP CONDITIONS:** Unknown legacy ownership or any unscoped mailbox target remains.
- **COMMIT / MERGE GATE:** Isolation commit before enabling four real accounts.

### T26 — Verify four real accounts and failure isolation

- **PRIORITY:** P0. **OWNER:** GROK_LOCAL. **CURRENT STATE:** UNVERIFIED.
- **DEPENDENCIES:** T25; HUMAN authorizes four accounts and OAuth consent.
- **SCOPE:** Gate 7 with local browser/OAuth and provider evidence.
- **EXACT FILES OR MODULES:** Per-account authorization script/config, tokens in protected local storage, sync/status/query/MCP surfaces.
- **REQUIRED CHANGES:** Authorize and verify each configured identity, independently sync/resume, disable one and simulate/revoke a test account credential with permission. Record only redacted identities and outcomes.
- **FORBIDDEN CHANGES:** No credentials sent to cloud, inferred consent or disrupting business tokens without explicit permission.
- **ACCEPTANCE CRITERIA:** Four account identities proven; no cross-account results; one auth failure leaves others operable; restart retains correct cursor/health ownership.
- **REQUIRED TESTS:** Local account selection, sync/read isolation, restart, revoke/reauthorize and all-account explicitly labeled view.
- **REQUIRED EVIDENCE:** Four-row identity/scope/health table, account-specific command/cursor IDs and negative isolation checks.
- **STOP CONDITIONS:** Fewer than four usable authorized accounts, identity mismatch or leakage.
- **COMMIT / MERGE GATE:** Gate 7 PASS; not permission to send.

## M8 — governed communication and follow-up

### T27 — Add durable reviewed local and provider drafts

- **PRIORITY:** P0. **OWNER:** CLAUDE_CLOUD. **CURRENT STATE:** MISSING.
- **DEPENDENCIES:** T26 and T23.
- **SCOPE:** Create/edit/save/reload draft, reply, reply-all and forward with bounded attachments.
- **EXACT FILES OR MODULES:** `providers/gmail.py`, `providers/fake_gmail.py`, `gmail_schemas.py`, `gmail_db.py`, email routes/templates, auth script; NEW `services/email_draft_service.py`, `tests/test_gmail_drafts.py`, draft migration.
- **REQUIRED CHANGES:** Version local drafts with account, source thread/message, recipients, body/attachment hashes and provider draft ID. Mirror to Gmail only by explicit operator action. Request readonly+compose only for authorized accounts. Persist retryable/unknown provider-save state. Construct MIME and thread/header metadata correctly; reply-all honors Reply-To, excludes all own aliases, deduplicates, never propagates received Bcc. Forward thread behavior must match the selected action/provider semantics and be tested, not assume reply membership.
- **FORBIDDEN CHANGES:** No model draft-write provider capability, auto-save to Gmail merely on generation, send or gmail.modify scope.
- **ACCEPTANCE CRITERIA:** Draft version/body/attachments survive restart and provider reload; stale/conflicting provider edits are surfaced; correct account/recipient/thread behavior.
- **REQUIRED TESTS:** New/reply/all/forward, self aliases/Bcc, attachments, provider draft timeout/conflict, version checks and restart.
- **REQUIRED EVIDENCE:** Draft state contract, MIME/header assertions, fake round-trip traces and full tests.
- **STOP CONDITIONS:** Insufficient consent, unknown provider draft outcome or cross-account parent.
- **COMMIT / MERGE GATE:** Draft-only commit; no send path enabled.

### T28 — Implement explicit version-bound send and reconciliation

- **PRIORITY:** P0. **OWNER:** CLAUDE_CLOUD. **CURRENT STATE:** MISSING.
- **DEPENDENCIES:** T27.
- **SCOPE:** Typed operator-only Gmail sending with durable recovery.
- **EXACT FILES OR MODULES:** Draft/provider modules, `app.py`, `security.py`, command-log repository; NEW `services/email_send_service.py`, `tests/test_gmail_send_authority.py`, `tests/test_gmail_send_reconciliation.py`, send-attempt migration.
- **REQUIRED CHANGES:** Persist preview/approval bound to account, exact draft version, recipients, subject/body/attachment hash and idempotency key. Any edit invalidates approval. Claim one send attempt atomically, then call provider outside DB transaction. Persist result or unknown outcome; reconcile provider ID/thread/SENT before displaying sent. Prevent concurrent/replayed send and reject consumed approval. Use the saved provider draft where possible; correlation metadata is reconciliation evidence, not a provider exactly-once guarantee. Unknown outcomes require readback/operator recovery before any retry.
- **FORBIDDEN CHANGES:** No `/ask`/MCP/model send route, automatic approval, blind timeout retry or false “sent”.
- **ACCEPTANCE CRITERIA:** Unapproved/stale/mismatched requests make zero provider sends; approved request sends once under tested races/replay; crash/timeout cannot trigger automatic resend; sent is provider-confirmed.
- **REQUIRED TESTS:** Origin/Host, forged/stale approvals, concurrent clicks, pre/post-provider crash, timeout after acceptance, retry same key/different content, reconciliation mismatch and revoked credentials.
- **REQUIRED EVIDENCE:** Authority/state diagram, immutable snapshot, attempt/command IDs, fake-provider call counts and full tests.
- **STOP CONDITIONS:** Ambiguous provider outcome, replay can send twice or approval snapshot incomplete.
- **COMMIT / MERGE GATE:** Governed-send commit; remains unverified live until T30.

### T29 — Add local LLM draft assistance and governed email follow-up tasks

- **PRIORITY:** P1. **OWNER:** CLAUDE_CLOUD. **CURRENT STATE:** PARTIAL.
- **DEPENDENCIES:** T28, T17, T18, T03.
- **SCOPE:** Complete missing user workflows using existing draft, classification and task governance.
- **EXACT FILES OR MODULES:** `services/email_classification_service.py`, draft service, `services/write_proposals.py`, `tools/write_inputs.py`, email/lead views; NEW `services/email_assistance_service.py`, `services/email_followup_service.py`, `tests/test_email_assistance.py`, `tests/test_email_followup_proposals.py`; task provenance migration only if needed.
- **REQUIRED CHANGES:** Generate bounded local draft suggestions from selected account/thread and permitted CRM/Community evidence; label inference and preserve evidence references. Operator explicitly accepts a suggestion into a local draft; provider mirror/send remain separate actions. Convert validated follow-up markers into proposed CRM tasks with account/message/thread/source IDs, unambiguous target and timezone-resolved due date. Reuse preview/approve/apply and a stable evidence-based idempotency key; ambiguous dates/identity require review.
- **FORBIDDEN CHANGES:** No automatic task or draft/send from markers, external model, hidden cross-account context, or model altering recipients/approval authority.
- **ACCEPTANCE CRITERIA:** Suggestion alone causes zero provider action; accepted draft preserves review/version controls; approved follow-up creates exactly one task with evidence; repeat sync/proposal/replay adds none.
- **REQUIRED TESTS:** Injection, model failure, unsupported claims, account-bound context, recipient separation, ambiguous target/date, timezone/DST, approval/replay and task rollback.
- **REQUIRED EVIDENCE:** Suggestion/draft/proposal/applied-task distinctions, source chain, command IDs and full tests.
- **STOP CONDITIONS:** Identity/date ambiguous or model-generated assertions appear verified.
- **COMMIT / MERGE GATE:** Assistance and task bridge as small independent commits; Gate 8 still required.

### T30 — Certify controlled drafting, send/reply and follow-up

- **PRIORITY:** P0. **OWNER:** GROK_LOCAL. **CURRENT STATE:** UNVERIFIED.
- **DEPENDENCIES:** T29; HUMAN approval of each named test send and scope consent.
- **SCOPE:** Gate 8, operator-controlled recipient and test mailbox first.
- **EXACT FILES OR MODULES:** Local model, draft/review/send UI, provider drafts/readback, task proposal/apply and communication history.
- **REQUIRED CHANGES:** Demonstrate model suggestion → local review → provider draft → restart/edit → new approval → one send/reply → provider reconciliation → local history. Exercise reply-all/forward/attachment cases with controlled addresses. Create and approve one evidence-linked follow-up task, then replay. Check send-account selection independently for all four accounts within explicit test authorizations.
- **FORBIDDEN CHANGES:** No autonomous send, unapproved recipient, business outreach or resending unknown attempt.
- **ACCEPTANCE CRITERIA:** Provider confirms expected sender, recipient, message/thread/headers/SENT; recipient receives one expected message; edited approval is rejected; unknown/replay behavior safe; task exists once with command/provenance.
- **REQUIRED TESTS:** Live controlled send cases, denied unapproved request, restart/reload, task replay, per-account sender identity; fake crash tests remain mandatory.
- **REQUIRED EVIDENCE:** Approval/draft/version/attempt/command IDs, sanitized provider/recipient confirmation and task receipt.
- **STOP CONDITIONS:** Missing explicit send approval, wrong sender/recipient/thread, duplicate or unknown delivery.
- **COMMIT / MERGE GATE:** Gate 8 PASS; no blanket permission for future sending.

## M9/M10 — system and operational completion

### T31 — Certify all three workflows on one integrated revision

- **PRIORITY:** P0. **OWNER:** GROK_LOCAL. **CURRENT STATE:** UNVERIFIED.
- **DEPENDENCIES:** T30 and Gates 1–7 on the same release candidate or revalidated changes.
- **SCOPE:** Gate 9; complete E2E A/B/C and cross-system evidence.
- **EXACT FILES OR MODULES:** All certified services, `/ask`, MCP, CRM/Community/Gmail/Knowledge views and source tables.
- **REQUIRED CHANGES:** Run E's matrix on a local release candidate with explicit operator permissions for any mailbox/write step. Trace one genuine discussion through classification/CRM evidence to company/person/stored communication, research/follow-up and an approved communication. Verify unchanged preexisting CRM and Knowledge reads.
- **FORBIDDEN CHANGES:** No combining evidence from different unvalidated SHAs, fake fixtures labeled live or deleting mismatched evidence.
- **ACCEPTANCE CRITERIA:** Every workflow completes with source/identity/account/version/command chain; cross-system answers cite stored evidence; inferred links remain labeled; real provider confirmation and unit results are separate.
- **REQUIRED TESTS:** Complete isolated suite plus real A/B/C runs and cross-account/injection/duplicate negatives.
- **REQUIRED EVIDENCE:** Certified SHA, per-gate manifest, entity/source/command/provider IDs, expected versus actual results.
- **STOP CONDITIONS:** Any previous gate invalidated, trace breaks or a scope/authority boundary fails.
- **COMMIT / MERGE GATE:** Gate 9 PASS; only evidence/runbook corrections unless defect returns to its owner task.

### T32 — Prove restart/recovery and record final local acceptance

- **PRIORITY:** P0. **OWNER:** GROK_LOCAL. **CURRENT STATE:** MISSING.
- **DEPENDENCIES:** T31.
- **SCOPE:** Gate 10 and HUMAN sign-off, not additional product features.
- **EXACT FILES OR MODULES:** `README.md`, operational config, versioned migrations, backup/restore procedure; NEW `docs/local-operations.md` and sanitized certification note.
- **REQUIRED CHANGES:** Document exact start/stop, model/server configuration, migrations, account authorization/recovery, failed-item recovery, unknown-send recovery, local backups and restore. Stop/restart app/model/MCP independently, interrupt safe test workflows and restore a backup into a disposable directory. Verify SQLite plus Knowledge originals and attachment references. Record final HUMAN acceptance and remaining conditional automated-access status.
- **FORBIDDEN CHANGES:** No destructive restore into user DB, tokens in reports, permanently broad scopes or claiming pending gates complete.
- **ACCEPTANCE CRITERIA:** Integrity/foreign keys pass; restore preserves records/source hashes/files; pending jobs/cursors/drafts/approvals/attempts recover safely; no duplicate ingestion/task/send; loopback and account isolation hold; all ten gates pass.
- **REQUIRED TESTS:** Cold start, model outage, MCP reconnect, interrupted sync/attempt recovery, backup restore, log redaction, denied external-origin mutation and full suite.
- **REQUIRED EVIDENCE:** Operator-run command/runbook transcript, integrity/count/hash comparison, recovery outcomes, release SHA and dated HUMAN sign-off.
- **STOP CONDITIONS:** Any data loss, unsafe resend, missing files, leaked secret/body, unresolved required gate or absent operator acceptance.
- **COMMIT / MERGE GATE:** Final operational certification only after Gate 10 PASS; code promotion remains reviewed and explicit.
