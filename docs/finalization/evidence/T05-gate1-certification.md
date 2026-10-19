# T05 / Gate 1 — certification record

**Status: PASS — certified locally by GROK_LOCAL** (operator report received 2026-10-19).

| Item | Value (as reported) |
|---|---|
| Certified SHA | `78b0cdf68d71b6533e95d23c865cd88de653010a` (code `d00c2c6`) |
| Platform | Windows 11, Python 3.12.10 |
| Suite | 369 passed, 23 skipped (optional PostgreSQL) |
| Isolation guard | 17 passed |
| Model | Qwen 3.5 9B via local LM Studio |
| Certified planner configuration | `LMSTUDIO_PLANNER_REASONING_EFFORT=none`, timeout 180 s, max tokens 512 |
| Scenarios | P1 PASS, P2 PASS, P3 PASS, D1 PASS, U1 PASS |
| HTML disclosure | PASS |
| SQLite copy integrity | PASS |
| Production database | unchanged |
| Evidence location (local, not in repository) | `C:\gate1\run-78b0cdf-20261019\` |

History: the first run at `5a5054c` failed P1 (`clarify`, 152 641 ms). Triage classified it as a
test-contract defect (ambiguous wording), and the operator approved a correction (`d00c2c6`):
stricter P1 arguments, no planner change. The Windows TestClient isolation fix is `414e104`.

Not supplied in the cloud report (kept in the local evidence folder): per-scenario `elapsed_ms`,
reasoning-probe figures and command ids. The planner is certified **only** for the configuration
above. T04 code is frozen. Later changes to `ask_router.py`, the registry, `command_service.py` or
`llm*.py` (including T06's merge) require focused Gate 1 re-verification under T08.
