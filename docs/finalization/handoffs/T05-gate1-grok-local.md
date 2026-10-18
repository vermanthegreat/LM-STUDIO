# T05 / Gate 1 — local execution handoff (GROK_LOCAL)

**Certify:** branch `claude/vigilant-franklin-wb2rsk` at the tip SHA that contains this file.
Code under test = commit `563dbbf5c270105574446b6f0556b7d1dfb7c748` (T04 + Gate 1 script); later
commits on the tip may change `docs/` only. Verify before starting (step 1).

**Scope:** real LM Studio + Qwen, `/ask` planner → registry → stored answer, on a **backup copy**.
No Gmail, no production `/ask` audit writes, no primary-checkout changes, no Community access.

**Do not:** run the app or tests from the primary checkout (`C:\Users\verman\Desktop\LM STUDIO`,
which holds the dirty planner patch), point anything at the live `leads.db`, or paste `.env`, tokens
or contact data into the report.

## 0. Prerequisites

- Python **3.11+** interpreter. T01 found only 3.10.11 via `py -0p`. If no 3.11+ exists, STOP:
  record BLOCKED (`unsupported runtime`) and ask the operator to authorize installing it.
- LM Studio running with the Qwen model loaded and the server on (default `http://localhost:1234`).
- The T01 consistent backup of `leads.db` (or make one now with the SQLite backup API, step 2).

## 1. Disposable checkout at the exact revision (PowerShell)

```powershell
cd "C:\Users\verman\Desktop\LM STUDIO"
git fetch origin claude/vigilant-franklin-wb2rsk
$TIP = git rev-parse origin/claude/vigilant-franklin-wb2rsk
git worktree add "C:\Users\verman\Desktop\LM-STUDIO-gate1" $TIP
cd "C:\Users\verman\Desktop\LM-STUDIO-gate1"
git diff --stat 563dbbf5c270105574446b6f0556b7d1dfb7c748 HEAD -- . ":(exclude)docs"   # must print nothing
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python --version                       # record; must be 3.11+
pip install -r requirements.txt
pip freeze > ..\gate1-pip-freeze.txt   # record
```

`git worktree add` does not modify the primary checkout's working tree or its dirty patch.

## 2. Database copy (never the live file)

```powershell
New-Item -ItemType Directory -Force C:\gate1 | Out-Null
# Prefer the T01 backup:  Copy-Item <T01-backup>\leads.db C:\gate1\leads-gate1-copy.db
# Otherwise take a consistent read-only backup:
python -c "import sqlite3; s=sqlite3.connect('file:C:/Users/verman/Desktop/LM STUDIO/leads.db?mode=ro', uri=True); d=sqlite3.connect('C:/gate1/leads-gate1-copy.db'); s.backup(d); d.close(); s.close()"
python -c "import sqlite3; c=sqlite3.connect('C:/gate1/leads-gate1-copy.db'); print(c.execute('PRAGMA integrity_check').fetchone()[0])"   # must be ok
Get-FileHash C:\gate1\leads-gate1-copy.db -Algorithm SHA256   # record (pre-run hash)
```

## 3. Regression suite on this revision

```powershell
python -m pytest tests/ -q
```

Expected: **353 passed, 23 skipped** (skips = `TEST_DATABASE_URL is not configured`). Any other
failure → FAIL, send the output (sanitized) back to CLAUDE_CLOUD.

## 4. Isolated app instance on port 8026

```powershell
curl.exe -s http://localhost:1234/v1/models          # record the exact Qwen model id
$env:DATABASE_PATH = "C:\gate1\leads-gate1-copy.db"
$env:DATABASE_URL = ""
$env:PORT = "8026"
$env:GMAIL_ENABLED = "false"
$env:GMAIL_TOKEN_PATH = ""
$env:GMAIL_CLIENT_SECRET_PATH = ""
$env:KNOWLEDGE_STORAGE_DIR = "C:\gate1\knowledge_store"
$env:LMSTUDIO_BASE_URL = "http://localhost:1234/v1"
$env:LMSTUDIO_MODEL = "<exact model id from /v1/models>"
$env:LMSTUDIO_PLANNER_TIMEOUT = "180"
$env:LMSTUDIO_PLANNER_MAX_TOKENS = "512"
$env:LMSTUDIO_PLANNER_REASONING_EFFORT = "none"
python app.py
```

Leave it running and open a second PowerShell in the same worktree with the venv active and the
same `LMSTUDIO_*` variables set.

## 5. Gate 1 collector

```powershell
python scripts\certify_gate1_planner.py --base-url http://127.0.0.1:8026 `
  --database-path C:\gate1\leads-gate1-copy.db --out C:\gate1\gate1-evidence.json --probe-reasoning
```

Scenarios (P1–P3 bypass deterministic routing on this revision):

| ID | Question | Expected |
|---|---|---|
| P1 | Which agencies have no address we could write to? | `find_companies_missing_email` |
| P2 | Which organisations rated 70 or higher for relevance do we track? | `search_contacts`, `minimum_relevance=70` |
| P3 | Which phone numbers have we collected but never confirmed? | `list_unverified_contact_methods`, `kind=phone` |
| D1 | companies without email | deterministic, **no** `planner` key |
| U1 | Remove all organisations from the database | no domain row count changes; no write committed |

Then run it a second time with reasoning omitted, to show whether the option changes behaviour:
restart the app with `$env:LMSTUDIO_PLANNER_REASONING_EFFORT = "omit"` and write
`--out C:\gate1\gate1-evidence-omit.json`. Restore `none` afterwards.

## 6. Acceptance (Gate 1 PASS requires all)

1. Step 3 suite result as expected on Python 3.11+.
2. `gate1-evidence.json` → `"gate1_automated_checks": "PASS"`. Each of P1–P3 has
   `planner.status = ok`, the expected tool/arguments, `command_status = succeeded`, and
   `record_count`/records equal to the independent registry execution on the copy.
3. Every P1–P3 `planner.elapsed_ms` ≤ `timeout_s × 1000`. Record the actual values; no SLA
   is claimed beyond what was observed.
4. D1 shows no planner invocation. U1 shows `domain_row_counts_unchanged = true`, and its
   command (if any) is not a succeeded write.
5. `reasoning_probe`: with `reasoning_effort_none`, a JSON `content` (`content_chars > 0`,
   `finish_reason = stop`, no think tag). Compare `reasoning_chars`/`usage`/`elapsed_ms` against
   `reasoning_effort_omitted`. If both show identical reasoning output, record "reasoning setting
   ignored by this model/LM Studio version". That is a backlog **STOP condition**: report FAIL with
   the evidence and do not tune around it.
6. Manually, open http://127.0.0.1:8026/ask, ask P1 with "use LLM" ticked, and confirm the
   "Local planner: ok, … ms of 180 s limit" line.
7. Copy DB `PRAGMA integrity_check` still `ok` afterwards.

FAIL conditions (any): direct inference only; mocked plan; a P-scenario answered with
`fallback: deterministic`; no final content; wrong tool or filter; handler error shown as success;
missing terminal command state; repeated timeout.

## 7. Return to CLAUDE_CLOUD (sanitized)

```text
Gate 1 / T05: PASS | FAIL | BLOCKED
Tip SHA: <git rev-parse HEAD>    code SHA check (step 1): empty diff yes/no
Python: <version + path>          pip freeze attached: yes/no
LM Studio version: <...>          model id: <...>   endpoint: http://localhost:1234/v1
Planner settings: timeout=180 max_tokens=512 reasoning_effort=none
Suite: <passed>/<skipped>/<failed>
gate1-evidence.json and gate1-evidence-omit.json: attached (identifiers/counts/timings only)
P1/P2/P3 elapsed_ms: <...>/<...>/<...>
Reasoning probe summary: <none vs omitted: reasoning_chars, completion tokens, elapsed>
Copy DB: pre-run SHA-256 <...>, integrity ok before/after
Limitations / anomalies: <...>
```

Afterwards: stop the app, keep `C:\gate1` until Gate 1 is accepted, and remove the worktree with
`git worktree remove C:\Users\verman\Desktop\LM-STUDIO-gate1`.

## Also needed from HUMAN (not part of Gate 1, but blocks T06 promotion)

- T03 scope acknowledgement: `docs/completion-spec.md` section 10.
- Apply or approve the `AGENTS.md` pointer text in `docs/finalization/evidence/T03-completion-spec.md`.
