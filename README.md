# LM Studio Lead Intelligence

Local manual-copy lead intelligence app. You copy text from Shopify Partner Directory, LinkedIn, websites, and emails — then paste into the web UI. By default, all data is stored in a local **SQLite** database (`leads.db` or `DATABASE_PATH`). The LLM (LM Studio) helps extract structure but is **not** treated as memory.

**No scraping.** This app does not scrape Shopify, LinkedIn, or any other site. No browser automation, no login automation, no mass email, no n8n, and no generic SQL-agent behavior.

## Quick start (SQLite — default)

```bash
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:8025

Do **not** set `DATABASE_URL` unless you are explicitly testing Phase 1 PostgreSQL foundation code. Without it, the app uses SQLite only.

Optional: run [LM Studio](https://lmstudio.ai/) with a model loaded at `http://localhost:1234/v1/chat/completions`. If LM Studio is unavailable, deterministic fallback parsers still save pasted text.

## Manual workflow

1. Open a Shopify Partner company page in your browser.
2. Select all (Ctrl+A) and copy (Ctrl+C).
3. Paste into the app, choose **Shopify Partner Directory**, click **Parse & Save**.
4. Open LinkedIn company or person pages, copy, paste with the matching source type.
5. Optionally attach pasted content to an existing company.
6. Paste email threads to create interactions and follow-up tasks.
7. Use **Ask Database** for counts and lists (answers from SQLite).
8. Export leads as CSV from the nav link.

## Pages

| Route | Purpose |
|-------|---------|
| `/` | Paste box + parse |
| `/leads` | Lead list |
| `/leads/{id}` | Lead detail |
| `/ask` | Natural-language DB queries |
| `/export/csv` | CSV download |

## Ask database examples

- `koliko imamo potencijalnih klijenata?`
- `contact summary` — companies, people, and email coverage
- `print all emails` / `list email addresses`
- `companies without email`
- `show top leads`
- `show leads without contacts`
- `show follow-ups due`
- `summarize company Acme Agency`

## Tests

```bash
python -m pytest tests/ -v
```

SQLite route and unit tests run without PostgreSQL. Optional PostgreSQL integration tests in `tests/test_pg_integration.py` are skipped unless `TEST_DATABASE_URL` points at a disposable database.

## Runtime

| Mode | When | Status |
|------|------|--------|
| **SQLite** (default) | `DATABASE_URL` unset; `DATABASE_PATH` optional | Phase 0 foundation — supported local runtime |
| **PostgreSQL** | `DATABASE_URL` set | Phase 1 foundation code in-tree; **experimental, not accepted** until Phase 1 acceptance criteria pass |

The web app reads and writes through a repository layer. SQLite remains the default and recommended runtime for local use on port **8025**.

## Legacy CLI (Postgres)

Older CLI tools (`agent.py`, `ingest_raw.py`) used PostgreSQL via `db_postgres.py`. That legacy stack is separate from the current web app repository layer. Do not confuse it with Phase 1 `persistence/` / `repositories/` code.

## Environment (optional)

```
APP_HOST=127.0.0.1
PORT=8025
DATABASE_PATH=./leads.db
MAX_PASTE_CHARS=200000
LMSTUDIO_BASE_URL=http://localhost:1234/v1
LMSTUDIO_MODEL=local-model
LMSTUDIO_TIMEOUT=60
LOG_LEVEL=INFO
```

Copy `.env.example` to `.env` and adjust paths for your machine. Never commit real credentials.

### PostgreSQL (Phase 1 — experimental, not production)

Phase 1 foundation code exists in this repository (SQLAlchemy models, repositories, Alembic baseline, migration CLI). **Phase 1 is not complete or certified** until its acceptance criteria in `docs/implementation-roadmap.md` are verified (repeatable migration, reconciliation report, isolated PG integration tests, and related checks).

Only enable PostgreSQL for disposable development or test databases:

```bash
# Optional: switch runtime (experimental)
export DATABASE_URL=postgresql://user:password@localhost:5432/contacts_dev

# Create schema (first run on a disposable DB)
python -c "from persistence.session import init_schema; import os; init_schema(os.environ['DATABASE_URL'])"

# Migrate existing SQLite data — ALWAYS dry-run first; back up valuable data first
python scripts/migrate_sqlite_to_postgres.py --sqlite-path leads.db --database-url "$DATABASE_URL" --dry-run
python scripts/migrate_sqlite_to_postgres.py --sqlite-path leads.db --database-url "$DATABASE_URL"

# Alembic baseline (future migrations)
DATABASE_URL=... alembic upgrade head
```

**Warning:** Do not run migration against production or valuable `leads.db` data without a dry-run, a backup, and a disposable PostgreSQL target. Review the JSON reconciliation report for skipped and conflicting records before trusting results.

PostgreSQL integration tests (optional; requires disposable `TEST_DATABASE_URL`):

```bash
TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/contacts_test python -m pytest tests/test_pg_integration.py -v
```
