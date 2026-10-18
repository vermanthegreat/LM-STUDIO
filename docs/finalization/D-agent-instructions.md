# D. Agent execution instructions

These instructions become operational only when the user authorizes implementation. The present task produced planning artifacts only. Do not forward prompts, create agent chats, commit, merge or access providers automatically.

## Shared handoff contract

Use [C-task-backlog.md](C-task-backlog.md) as the task contract and [E-acceptance.md](E-acceptance.md) as the release decision. Every handoff contains:

1. Task ID, phase, dependencies and their evidence IDs.
2. Exact branch/commit SHA and any working patch hash; never just a branch name.
3. Files changed, migrations/configuration required and forbidden paths/actions.
4. Test command, Python/dependency versions, test totals and skip/failure reasons.
5. Expected observable behavior and a local verification script/checklist using safe paths.
6. Sanitized results, identifiers/counts, command receipts and unresolved limitations.
7. `PASS`, `FAIL` or `BLOCKED` with the exact gate; no “looks good” acceptance.

Cloud owns implementation and fake-provider tests. Local owns workstation/model/browser/OAuth/real-capture/provider certification. HUMAN supplies captures, consents, named send approvals and final acceptance. A runtime verification failure returns to the owning code task with a minimal reproduction; Grok does not independently rewrite the same module.

Only re-open architecture questions when a recorded assumption is contradicted. Consult the relevant evidence/task first. Update the same task's evidence instead of creating a parallel implementation.

## Claude Cloud implementation instructions

Work on an isolated branch based on the verified main ref. Read `AGENTS.md` and the six specifications, then the task's branch-specific sources. Do not touch the dirty local checkout or the separate Community worktree. Preserve `816bbb0`, main's K0/K1/G2 and Community `5acee41` behavior as specified in B. Verify SHAs at execution time; stop and report drift rather than overwrite it.

Never load local `.env`, `gmail_token*.json`, client secrets, private database files, message bodies or private fixture bundles in cloud. Use synthetic/redacted evidence supplied through the handoff. Do not connect Gmail, LM Studio, production PostgreSQL or Community. No network-enabled integration tests; a documented package install is different from permission to access user providers.

Sequence:

| Step | Implement | Then hand off / wait |
|---|---|---|
| 1 | T02 safe supported-runtime baseline; T03 narrow completion spec | Wait for baseline preservation and required HUMAN scope acknowledgement; resolve actual isolated baseline failures. |
| 2 | T04 planner correction, preserving certified parts of local dirty patch | T05 must pass on real local Qwen before calling it fixed. If it fails, revise T04 only. |
| 3 | T06 contact/main integration, T07 Community integration | T08 copy-based migration/regression. Keep integration on its isolated branch until passed. |
| 4 | After HUMAN T09, implement T10 only for demonstrated parser differences, then T11 atomicity | T12 genuine capture persistence. No extra parser/collector invention. |
| 5 | T13 reads, T14 analysis, T15 MCP | T16 real local model and client proof. |
| 6 | T17 candidate promotion/contact evidence, T18 CRM links | T19 complete contact chain. |
| 7 | Consume T20 failure taxonomy; T21 recovery, T22 history/convergence, T23 MIME | T24 authorized real synchronization. |
| 8 | T25 account configuration and all-surface isolation | T26 four-account OAuth/runtime proof. |
| 9 | T27 drafts, T28 operator send, T29 assistance/task bridge | T30 controlled sends and task proof. |
| 10 | Repair only reproducible defects from T31/T32; finalize operational docs | Local final certification and HUMAN acceptance. |

Before any suite, establish isolation: disposable checkout, explicit temporary `DATABASE_PATH`, `DATABASE_URL`/`TEST_DATABASE_URL` absent unless the latter is an explicitly disposable test DB, Gmail disabled, no real token/client-secret paths and no imported private data. A fake provider must be the only provider callable by automated tests. Avoid relying only on monkeypatching a module constant when Python default arguments have already bound the real path. Carry main's fallback-query test fix and enforce default-path rejection.

Recommended verification groups, after isolation is proven:

```text
python -m pytest tests/test_ask_llm_planner.py tests/test_ask_planner_validation.py tests/test_ask_planner_loop_read_only.py tests/test_command_log.py tests/test_tool_registry.py -q
python -m pytest tests/test_candidate_persistence.py tests/test_research_job_runner.py tests/test_write_proposal_authority_and_replay.py tests/test_write_proposal_apply_atomicity.py -q
python -m pytest tests/test_gmail_g0.py tests/test_gmail_full_sync_provider.py tests/test_gmail_full_sync_resume.py tests/test_gmail_conversation_linking.py tests/test_gmail_communication_state.py -q
python -m pytest tests/test_knowledge_ingestion.py tests/test_knowledge_k1_embeddings.py tests/test_knowledge_search_and_routes.py -q
python -m pytest tests/test_community.py tests/test_community_discovery.py -q
python -m pytest tests/ -q
```

Run groups only where those modules exist on the relevant baseline/integrated commit; add each task's new tests before its full-suite run. Use Python 3.11+; do not modify code to make unsupported Python 3.10 an alternative completion platform. Existing PostgreSQL tests can remain explicitly skipped without a disposable test service because production PostgreSQL acceptance is outside this plan. A skipped SQLite/registry/Gmail-fake/Community/Knowledge completion test is not acceptable.

Keep changes per task small. Do not replace whole files on merge conflict, rebuild the schema, create another generalized framework, reimplement ingestion, or add optional capability to “finish” the project. Update spec/config with behavior. Persist migrations separately from runtime reads, and prove rollback or restore on disposable data. Changes to a shared registry, repository, command state or schema invalidate affected earlier evidence and require focused re-certification.

Cloud cannot certify local model latency/options, real `/ask` tool choice, desktop MCP client launch, Chrome capture compatibility, OAuth consent/token identity, real mailbox completeness, actual Gmail draft/thread/send/recipient results, private DB reconciliation or workstation restart/restore. Passing mocks does not close those gates.

## Grok Local verification instructions

Start with T01 preservation. Do not run application imports/startup against the user's database merely to inspect it: lifespan and status helpers can initialize/mutate schema or load credentials. Read source first; use consistent backup copies and isolated config. Keep `.env`, OAuth files and private mailbox content out of reports and cloud handoffs.

The launcher inventory in this assessment listed only Python 3.10.11. Before running the accepted suite or app, identify an existing supported interpreter or obtain operator authorization to install Python 3.11+ and create an isolated environment. Report the exact executable; do not silently run the wrong interpreter. Use the same package manifest as the tested cloud commit.

Sequence:

1. **T01:** Manifest, dirty diff preservation and consistent backup/restore check; reconcile full audit reports if accessible. No provider calls.
2. **T05 / Gate 1:** On a copy, run actual Qwen requests and prove planner-selected tool execution. Record configuration, final content, tool validation, result counts and audit IDs separately. Verify the request option actually changes behavior; a comment or HTTP 200 is insufficient.
3. **T08 / Gate 2:** Verify combined routes/tools/tests, migration order and copy reconciliation. Check that contact/Gmail, K0/K1 and Community survived integration. Return code defects to cloud.
4. **T12 / Gate 3:** Use HUMAN T09 genuine captures locally; compare parser output with original source evidence, replay and restart. Supply only authorized redacted fixtures to cloud.
5. **T16 / Gate 4:** Run real Community analysis and `/ask`; initialize/list/call through a real MCP consumer. Document the actual client. Check denied calls and nonmutating read effects.
6. **T19 / Gate 5:** Demonstrate reviewed company/person/contact/discussion/email linkage, dedup, research and task visibility on copy data. No live Gmail needed.
7. **T20:** Investigate historical errors offline; separate unavailable evidence from proof. This can occur after Gate 2 without waiting for Community tasks.
8. **T24 / Gate 6:** Obtain explicit authorization for named mailbox read/sync. Test account first; full/history/recovery/reconciliation next. No send or remote mutations.
9. **T26 / Gate 7:** HUMAN authorizes four-account OAuth. Verify account/token/profile mapping, identical-ID isolation and independent revocation/recovery. Redact account names in shareable notes.
10. **T30 / Gate 8:** HUMAN approves each exact send snapshot to operator-controlled recipients. Test local draft/model assistance, provider mirror, restart, invalidated approval, one send and provider/recipient confirmation. Never replay an uncertain attempt.
11. **T31 / Gate 9:** Run all workflows on the same release candidate. Recheck only gates affected by fixes, plus final combined regressions.
12. **T32 / Gate 10:** Cold restart, outage/reconnect, safe interruption, restore and integrity checks. Present the evidence matrix for HUMAN acceptance.

For every runtime procedure: record declared dataset/account scope, starting state, action, expected effects, observed effects, exact command/draft/provider/source IDs and omissions. Keep message IDs private when appropriate; share only stable redacted references. Do not claim negative evidence for records outside ingested scope.

If a procedure needs new authorization, prepare all safe offline work and its exact reviewable action first. A token file's existence is not consent. Read-only OAuth does not permit send. Creating a local suggestion is not permission to mirror a Gmail draft, send it or apply a CRM task. An unknown send outcome requires reconciliation, never a retry to see what happens.

## HUMAN decisions and inputs

- Confirm selected completion scope in T03; PostgreSQL, Calendar, generic search and autonomous actions remain excluded.
- Supply genuine captures under T09 and record permitted access. Automated collection may remain unresolved without blocking the manual E2E.
- Authorize named Gmail accounts/scopes at T24/T26, provider draft writes and each exact send at T30; interact with OAuth locally.
- Accept any evidenced permanently unavailable historical items explicitly; do not waive unexplained recoverable loss.
- Sign final Gate 10 acceptance only after the release manifest is complete.
