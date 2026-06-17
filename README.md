# LM Studio Lead Intelligence

Local manual-copy lead intelligence app. You copy text from Shopify Partner Directory, LinkedIn, websites, and emails — then paste into the web UI. All data is stored in a local **SQLite** database (`leads.db`). The LLM (LM Studio) helps extract structure but is **not** treated as memory.

**No scraping.** This app does not scrape Shopify, LinkedIn, or any other site. No browser automation, no login automation, no mass email.

## Quick start

```bash
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:8000

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
LMSTUDIO_ENDPOINT=http://localhost:1234/v1/chat/completions
LMSTUDIO_MODEL=local-model
PORT=8000
```
