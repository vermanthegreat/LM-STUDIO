# T04 evidence — smallest planner correction (candidate fix)

- **Task:** T04 (M1). **Owner:** CLAUDE_CLOUD. **Depends on:** T02 (`c6a81a7`), T01's preserved patch.
- **Base:** `main` `b88bc5e` + handoff `949e093` + T02 + T03, branch `claude/vigilant-franklin-wb2rsk`.
- **Status:** **PASS (cloud tests) — candidate fix only.** The planner is *not* declared fixed
  until T05 / Gate 1 passes on real local Qwen.

## Preserved patch review (`docs/finalization/qwen-planner.patch`)

SHA-256 verified: `f5ab1c91611235f3d78916c88115a68f0fd0e337dccc3351ddf7046582360033`.
`llm.py`, `services/llm_planner.py` and `tests/test_ask_llm_planner.py` are byte-identical on
`main` and contact `816bbb0`, so the patch applies to both. It was applied first, then revised:

| Patch element | Decision | Reason |
|---|---|---|
| Planner-only timeout 8 s → 180 s | **Adopted, made configurable** (`LMSTUDIO_PLANNER_TIMEOUT`, default 180) | Planner-only bound, no global inflation. The "about 128 s" comment is not evidence, so it was dropped; Gate 1 records the measured latency. |
| `reasoning_effort="none"` for planner | **Adopted, made configurable** (`LMSTUDIO_PLANNER_REASONING_EFFORT`, `omit` disables) | Treated as an option that must be proven locally (Gate 1 `--probe-reasoning`). |
| `reasoning_effort` kwarg on `call_lmstudio_for_text` | **Not adopted** | The planner now uses `call_lmstudio_planner`; no other caller needs it. Classifier/polish calls are unchanged. |
| Tests: reasoning-off request, content-only parsing, text-call omission | **Adopted, rewritten** for the new call path | Same intent, now against the actual HTTP payload. |

## Changes

| File | Change |
|---|---|
| `llm.py` | New `call_lmstudio_planner` → `PlannerCallOutcome`: parses final `content` only (one JSON object, optional code fence); classifies timeout, unavailable, HTTP error, malformed response, empty, reasoning-only, `<think>` in content, truncated (`finish_reason=length`), malformed JSON. |
| `config.py`, `.env.example` | `LMSTUDIO_PLANNER_TIMEOUT`, `LMSTUDIO_PLANNER_MAX_TOKENS`, `LMSTUDIO_PLANNER_REASONING_EFFORT`. |
| `services/llm_planner.py` | `run_local_planner` → `PlannerOutcome` with `metadata()`; catalog shows risk class plus a compact argument signature from each Pydantic input model; `plan_question_with_local_llm` kept as a wrapper. |
| `ask_router.py` | Uses `run_local_planner`; returns `planner` metadata (ok or failed with `error_code`, `elapsed_ms`, `timeout_s`, `fallback: deterministic`); audits clarifications in the command log. |
| `services/command_service.py`, `tools/registry.py` | Error envelope → `failed` (never `succeeded`); unexpected handler exception → `failed` + `ToolExecutionError` (never stuck `executing`; message holds no private data); read summaries persist counts, status, warnings, provenance and record identifiers only. Write-proposal payloads unchanged. |
| `services/planner_validation.py` | Planner read path reports an error envelope as `tool_error`. |
| `templates/ask.html` | One-line planner disclosure (status, error, elapsed vs limit, fallback). |
| `docs/tool-contracts.md`, `docs/architecture.md` | Planner settings, failure codes, `planner` object, command-state and audit-summary rules. |
| `tests/test_ask_llm_planner.py` | 35 tests: see below. `tests/test_tool_contract_filters.py`, `tests/test_phase3_pg_e2e.py` patch the new entry point. |
| `scripts/certify_gate1_planner.py` | Operator-run Gate 1 evidence collector (T05). |

## Required tests → coverage (`tests/test_ask_llm_planner.py`)

| Required | Test(s) |
|---|---|
| Reasoning-only | `test_reasoning_channel_is_never_parsed`, failure param `planner_reasoning_only` |
| Invalid / empty / truncated output | `test_planner_failures_are_attributed` (empty, None, truncated ×2, think-in-content, non-object, invalid plan, extra fields, unknown action) |
| Timeout | param `planner_timeout`; fallback test `planner_timeout` disclosed |
| Unknown tool / fields | `test_invalid_plans_execute_nothing_and_are_rejected` (unknown tool, `sql` field, out-of-range limit) |
| Tool error envelope / exception | `test_handler_error_envelope_is_reported_as_failure`, `test_handler_exception_leaves_failed_terminal_state`, `test_unexpected_exception_raises_typed_error_from_service` |
| Clarification audit | `test_clarification_is_audited` |
| Deterministic bypass | `test_use_llm_true_does_not_call_planner_for_deterministic_intent`, `test_use_llm_false_does_not_call_local_planner` |
| Count accuracy | `test_valid_plan_executes_once_with_counts_and_terminal_audit` |
| Private-result log redaction | `test_read_command_log_keeps_ids_and_counts_not_private_records`, exception-message test |
| Bounded options / schemas | `test_planner_request_uses_bounded_planner_options`, `test_reasoning_effort_can_be_omitted`, `test_planner_prompt_lists_argument_schemas`, `test_planner_config_from_env`, `test_other_lm_studio_calls_keep_their_own_timeout` |
| Propose cannot mutate | `test_propose_tool_selected_by_planner_only_previews` |
| HTTP disclosure | `test_ask_routes_disclose_planner_outcome` |

## Results (Python 3.11.17 / 3.12, isolated by `tests/conftest.py`)

```text
D-group: test_ask_llm_planner, test_ask_planner_validation, test_ask_planner_loop_read_only,
         test_command_log, test_tool_registry           -> 54 passed
full suite, 3.11.17                                      -> 353 passed, 23 skipped (optional PG)
full suite, 3.12                                         -> 353 passed, 23 skipped (optional PG)
```

Gate 1 script smoke test (cloud, **fake** OpenAI-compatible server, synthetic DB copy, *not*
certification): all 5 scenarios PASS with the fake planner; with the model server stopped,
P1–P3 FAIL with `planner_unavailable` + `fallback: deterministic` disclosed, D1/U1 PASS.

## Known integration note (for T06)

Cherry-picking this commit onto contact `816bbb0` conflicts in `ask_router.py`,
`docs/tool-contracts.md` and `templates/ask.html`, all shared with contact's own changes. T06
resolves these semantically when contact is merged into this line.
