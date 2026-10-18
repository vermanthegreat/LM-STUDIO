# T02 evidence — isolated Python 3.11+ test baseline

- **Task:** T02 (M0). **Owner:** CLAUDE_CLOUD. **Depends on:** T01 (reported GO by operator).
- **Date:** 2026-10-18 (UTC). **Environment:** Claude Cloud container; no `.env`, no OAuth files, no
  user database, no LM Studio, no Gmail, no PostgreSQL service.
- **Baseline refs verified:** main `b88bc5e`; contact `816bbb0`; handoff package `949e093`.

## Runtime

| Item | Value |
|---|---|
| Interpreters | CPython 3.11.17 (`/usr/bin/python3.11`), CPython 3.12 (`/usr/bin/python3.12`) |
| Environment | `uv venv -p python3.11` + `pip install -r requirements.txt` (no pins changed) |
| Key packages | alembic==1.20.0 fastapi==0.143.3 httpx==0.28.1 pydantic==2.14.1 pytest==9.1.1 sqlalchemy==2.1.5 starlette==1.8.0  |

## Isolation added

`tests/conftest.py` (new) runs before any test module import and:

1. pins `DATABASE_PATH`/`KNOWLEDGE_STORAGE_DIR` to a temp dir, blanks `DATABASE_URL`,
   `GMAIL_TOKEN_PATH`, `GMAIL_CLIENT_SECRET_PATH`, `LMSTUDIO_API_KEY`, forces Gmail/remote
   embeddings off — so `load_dotenv()` in `app.py` etc. cannot inject a real `.env`;
2. refuses `sqlite3.connect` / `sqlite3.dbapi2.connect` to repo `leads.db` or the shell's
   `DATABASE_PATH` (catches `db.DB_PATH` default arguments bound at import);
3. refuses all INET/INET6 socket connects (LM Studio, Gmail, PostgreSQL, web) except the
   host/port of a shell-supplied `TEST_DATABASE_URL` whose database name contains `test`;
4. fails the session if the default `leads.db` was created or modified.

`tests/test_isolation_guard.py` (new, 10 tests) covers the guard itself.
`.env.example` no longer has active `DATABASE_URL`/`TEST_DATABASE_URL` assignments (a copied
`.env` previously switched the app to PostgreSQL and could point PG tests, which run
`DROP SCHEMA public CASCADE`, at a non-disposable database). README documents Python 3.11+ and
venv commands. `.github/workflows/tests.yml` (new; none existed) runs the suite on 3.11 and 3.12.

## Results (`python -m pytest tests/ -q`, disposable worktrees)

| Checkout | Interpreter | Result | Notes |
|---|---|---|---|
| main `b88bc5e`, before T02 | 3.11.17 | 312 passed, 23 skipped | no `leads.db` created |
| contact `816bbb0`, before T02 | 3.11.17 | 3 failed, 561 passed, 23 skipped | **created `leads.db` in checkout** |
| contact `816bbb0` + T02 guard | 3.11.17 | 3 failed, 571 passed, 23 skipped | the 3 fail with `IsolationViolation`; no `leads.db` |
| T02 branch (main + guard) | 3.11.17 | 322 passed, 23 skipped | |
| T02 branch (main + guard) | 3.12 | 322 passed, 23 skipped | |

**Skips (23, all declared optional PostgreSQL):** `TEST_DATABASE_URL is not configured` in
test_command_log_persistence, test_migration_repeatability, test_pg_integration,
test_pg_schema_initialization, test_pg_task_idempotency_migration, test_phase3_pg_e2e,
test_shopify_pg_contact_projection, test_verified_email_truthfulness. Allowed per D/E.

## Baseline failure classification

| Branch | Failing tests | Cause | Resolution |
|---|---|---|---|
| contact `816bbb0` | `test_query_counts.py::test_route_llm_fallback_{unknown_question,invalid_json,unknown_intent}` | Call `route_question` without `db_path` → default `leads.db` (A/E17). Main fixed this in `7c8f47b`, which contact lacks. | Carried by the T06 merge (main's version of the file); guard now makes any regression fail loudly. |
| main `b88bc5e` | none | — | — |

## Status

**PASS (cloud scope).** Supported interpreter identified; suites use temporary stores and fakes;
baseline failures classified. Local (GROK) should re-run the suite with Python 3.11+ on the
workstation — A/E17 found only Python 3.10.11 via `py -0p`.
