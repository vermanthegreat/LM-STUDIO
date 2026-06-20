# LM Studio Lead Intelligence

Local manual-copy lead intelligence app. You copy text from Shopify Partner Directory, LinkedIn, websites, and emails — then paste into the web UI. All data is stored in a local **SQLite** database (`leads.db`). The LLM (LM Studio) helps extract structure but is **not** treated as memory.

**No scraping.** This app does not scrape Shopify, LinkedIn, or any other site. No browser automation, no login automation, no mass email.

## Quick start

```bash
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:8025

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

## Legacy CLI (Postgres)

Older CLI tools (`agent.py`, `ingest_raw.py`) used PostgreSQL via `db_postgres.py`. The web app uses SQLite via `db.py`. To use the legacy CLI, restore Postgres imports or point those scripts at `db_postgres.py`.

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

### PostgreSQL (Phase 1)

When `DATABASE_URL` is set, the app uses PostgreSQL via the repository layer instead of SQLite.

```bash
# Create schema (first run)
python -c "from persistence.session import init_schema; import os; init_schema(os.environ['DATABASE_URL'])"

# Migrate existing SQLite data (dry run first)
python scripts/migrate_sqlite_to_postgres.py --sqlite-path leads.db --database-url "$DATABASE_URL" --dry-run
python scripts/migrate_sqlite_to_postgres.py --sqlite-path leads.db --database-url "$DATABASE_URL"

# Alembic baseline (future migrations)
DATABASE_URL=... alembic upgrade head
```

PostgreSQL integration tests (optional, requires disposable `TEST_DATABASE_URL`):

```bash
TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/contacts_test python -m pytest tests/test_pg_integration.py -v
```
