# T06 plan — integrate contact intelligence into the certified main line

**Status: PLAN ONLY. Not executed.** Execution waits for the preconditions below. A disposable trial
merge was run in a scratch worktree to measure conflicts; nothing from it was committed or pushed.

## Inputs (verified 2026-10-19)

| Ref | SHA |
|---|---|
| `origin/main` | `b88bc5eeef50608d03e9b34293afb6f36a533b3d` |
| Base line (this branch, Gate 1 certified) | `78b0cdf68d71b6533e95d23c865cd88de653010a` = main + handoff + T02–T05 |
| Contact `origin/agent/contact-intelligence-hardening` | `816bbb071fefb79008b9c7208cd383335df37555` |
| Contact / main merge base | `8528eec28cd6015d75c82d9a59fc6acb01c5ceb4` |
| Community (T07, not in T06) | `5acee41ef7fc62f1ab024e4f12e99ee9eb02fa45` |

## Preconditions (all required before execution)

1. Gate 1 PASS at `78b0cdf`: **met** (`evidence/T05-gate1-certification.md`).
2. T03 HUMAN scope acknowledgement (`docs/completion-spec.md` section 10): **outstanding**.
3. `AGENTS.md` pointer text (`evidence/T03-completion-spec.md`), applied or approved by HUMAN:
   **outstanding**.
4. Refs unchanged from the table above at execution time. If any has moved, stop and re-plan.
5. Integration branch decision (HUMAN): **decided 2026-10-19:** `claude/vigilant-franklin-wb2rsk`;
   no `codex/finalization-integration` branch unless HUMAN says so; nothing to `main` before Gate 2.
6. Only committed refs are merged. The uncommitted local planner edits in the operator's
   contact-branch checkout (`llm.py`, `services/llm_planner.py`, `tests/test_ask_llm_planner.py`,
   i.e. the preserved patch) are **excluded** from T06. Their reviewed form is already in T04.

## Branch

Backlog B/D name a separate `codex/finalization-integration` branch. This session may push only to
`claude/vigilant-franklin-wb2rsk`, which is already isolated from `main` and carries the certified
T02–T05 commits. **Decided:** perform T06 on this branch. The merge commit's first parent will be the branch tip,
a docs-only descendant of `78b0cdf` (currently `257b77c` or later). Verify before merging with
`git diff --stat 78b0cdf HEAD -- . ':(exclude)docs'` (must be empty), so the code side is exactly
the certified `78b0cdf`. A separate branch needs explicit permission to create and push it. `main` is not touched;
promotion waits for Gate 2 (T08).

## Procedure

1. `git fetch`; verify the four SHAs.
2. `git merge --no-ff origin/agent/contact-intelligence-hardening`. This is a merge, not a squash
   or rebase, so the contact history (`816bbb0`, `ecd9a29`, …) is preserved and the T04 planner
   commits stay as their own commits.
3. Resolve the three textual conflicts as in the next table, and apply the one semantic fixture
   correction.
4. Run the targeted groups, then the full suite on 3.11 and 3.12; regenerate the inventories.
5. Write `evidence/T06-integration.md` (parents, resolutions, inventories, results); commit; push.
6. Hand Gate 2 (T08) to GROK_LOCAL **after T07**. T06 alone is not promoted.

## Trial merge results (`78b0cdf` + `816bbb0`, scratch only)

Auto-merged cleanly: 78 files, including `app.py`, `ask_router.py`, `config.py`, `db.py`,
`README.md`, `.gitignore`, `tests/test_query_counts.py` and five shared templates. Checked
after auto-merge:

- T04 planner path intact (`run_local_planner`, `_disclose`, clarification audit).
- Main's Knowledge routing (`knowledge_search`) intact.
- Main's `7c8f47b` fallback-test isolation kept (fallback tests use `tmp_path` databases).
- No `leads.db` was created.

**Textual conflicts (3), resolution = keep both:**

| File | HEAD side | Contact side | Resolution |
|---|---|---|---|
| `docs/tool-contracts.md` | T04 command-state and audit-summary rules | `response_spec` / `result_metadata` | Both sections, HEAD first |
| `static/app.css` | Knowledge ingestion styles | Gmail page mobile rules | Both blocks |
| `templates/ask.html` | T04 planner disclosure line | Interactive lead-result cards, else `<pre>` answer | Contact's result block, then the planner disclosure after it (shown for both branches) |

**Semantic conflict (1), found only by running the suite:**
`tests/test_knowledge_ingestion.py::test_person_entity_links_only_on_unique_exact_match` failed
(`'linked' == 'ambiguous'`).

- **Cause:** contact's `add_person` now deduplicates name-only people at the same lead. The
  Knowledge fixture `_seed_people` seeds two "Jane Twin" rows at one lead with no identifiers, so
  only one row exists and the name matches uniquely.
- **Resolution (test fixture only):** give each seeded person a distinct email. They then stay
  distinct people at the same lead, and the test's own invariants still hold (3 people, 1 lead,
  `Jane Twin` = `ambiguous`).
- Neither production behaviour changes: contact's dedup rule and K0's unique-exact-match linking
  are both preserved.

**Trial suite after these resolutions (Linux, 3.11.17):** 753 passed, 23 skipped, 0 failed.
The 23 skips are the optional PostgreSQL tests.

**Inventories (empty-database init / `create_app` / registry):**

| Inventory | main line | contact | trial merge | Lost |
|---|---|---|---|---|
| Registry tools | 10 | 9 | 10 | none (`search_knowledge` kept) |
| Routes | 29 | 21 | 30 | none (Knowledge routes and contact `POST /integrations/gmail/full-sync` both present) |
| SQLite tables | 22 | 19 | 32 | none (13 Knowledge + 10 contact/research/Gmail-mailbox tables) |

## Verification at execution

```text
python -m pytest tests/test_ask_llm_planner.py tests/test_ask_planner_validation.py tests/test_ask_planner_loop_read_only.py tests/test_command_log.py tests/test_tool_registry.py tests/test_gate1_scenarios.py tests/test_isolation_guard.py -q
python -m pytest tests/test_candidate_persistence.py tests/test_research_job_runner.py tests/test_write_proposal_authority_and_replay.py tests/test_write_proposal_apply_atomicity.py -q
python -m pytest tests/test_gmail_g0.py tests/test_gmail_full_sync_provider.py tests/test_gmail_full_sync_resume.py tests/test_gmail_conversation_linking.py tests/test_gmail_communication_state.py -q
python -m pytest tests/test_knowledge_ingestion.py tests/test_knowledge_k1_embeddings.py tests/test_knowledge_search_and_routes.py -q
python -m pytest tests/test_query_counts.py tests/test_routes.py tests/test_response_presentation.py tests/test_ask_interactive_leads.py -q
python -m pytest tests/ -q        # expect 753 passed, 23 skipped on 3.11 and 3.12
```

Acceptance: registry, route and table inventories equal the union above. Both pre-existing suites'
tests pass on the integrated code. The isolation guard creates no `leads.db`. The planner,
Knowledge and contact/Gmail behaviours are unchanged except for the documented fixture.

## Risks and re-certification

- The merge changes `ask_router.py` (contact's presentation and `list_leads` intent) and
  `templates/ask.html` around the certified planner. Under the backlog rule, shared-path changes
  need focused re-certification: Gate 1's P1–P3/D1/U1 collector must be re-run at the Gate 2 SHA
  during T08. T05 itself is not repeated.
- Contact's name-only person dedup changes how some identities merge. T17/T19 (Gate 5) must review
  it against the identity-tier rules in `completion-spec.md` section 4. It is recorded here; it is
  not changed in T06.
- `git apply`-style whole-file resolutions are forbidden. Any new conflict at execution time that
  is not listed above means stop and re-plan.
