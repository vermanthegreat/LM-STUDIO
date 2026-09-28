# Knowledge Ingestion (Phase K0)

Local drag-and-drop ingestion of arbitrary useful files, with full-text
retrieval and an evidence-bounded `/ask` path. SQLite runtime only.

The LLM is advisory. The original file and the deterministic extraction are
the source of truth; model output is schema-validated metadata and never
blocks persistence.

## Flow

```text
upload (POST /knowledge/ingest, multipart, per-file)
  -> size check, SHA-256, duplicate check (content_hash UNIQUE)
  -> deterministic router (magic bytes, then extension + content check)
  -> extraction: text decode | pypdf | DOCX XML (stdlib)
  -> images: VisionProvider (disabled unless KNOWLEDGE_VISION_MODEL is set)
  -> classification: LM Studio chat, bounded excerpt, Pydantic-validated
  -> original saved to knowledge_store/originals/<sha[:2]>/<sha256>.<ext>
  -> one SQLite transaction: item + topics + entities (+ exact links) + FTS row
```

Model calls happen before the transaction opens. Every file succeeds or
fails on its own: a batch of 20 with one unsupported file and one model
failure commits the other 18, and the model-failure item keeps its original
and extracted text (`classification_status = unavailable|invalid_output`).
Each batch writes one `command_log` entry (`tool_name = knowledge_ingest`)
with counts and item IDs only, never file contents.

## Supported types

| Kind | Types | Extraction |
|---|---|---|
| text | `.txt .md .json .csv .yaml .xml .html` and common source files (`.py .js .ts .go .rs .java .sql` …), or extension-less UTF-8 | safe decode (UTF-8/BOM/UTF-16/cp1252); JSON pretty-printed |
| document | PDF (`%PDF-` magic) | `pypdf` text layer; little or no text sets `needs_vision` |
| document | DOCX (zip containing `word/document.xml`) | paragraphs, headings (`#`), list items (`-`) and tables (`a \| b`) in document order |
| image | PNG, JPEG, WEBP (magic bytes) | vision provider, or stored as `needs_vision` |

Anything else, and any file whose content does not match its extension (for
example a `.png` that is not a PNG), is `unsupported` and nothing is stored.

## Images and vision

`KNOWLEDGE_VISION_MODEL` must name an LM Studio model that accepts image
input. When it is set, the image is sent as an OpenAI-style `image_url` data
URL and the model must return JSON `{visible_text, description, image_type}`.
Text screenshots are indexed primarily by their visible text; photos,
diagrams, and whiteboards put the visual description first.

When it is unset, or the model fails, the original is stored with
`vision_status = unavailable|failed` and no text. No OCR or description is
ever fabricated. Rendering scanned PDF pages for vision is not implemented.

## Storage

- Originals: `KNOWLEDGE_STORAGE_DIR` (default `./knowledge_store`, gitignored),
  content-addressed by SHA-256. User filenames are sanitized and stored only
  as metadata, never used as paths. Identical content is stored once.
- Download: `GET /knowledge/items/{id}/original` is always served as an
  `application/octet-stream` attachment with `nosniff`.

## Database (created additively by `db.init_db`)

| Table | Purpose |
|---|---|
| `knowledge_items` | one row per unique file: hash, source path, filename, MIME, kind, raw/normalized text, extraction/vision/classification status, summary, project (+source), category, sub_category, `event_date` (+`event_date_source = llm_inferred`), `captured_at`, importance, metadata JSON, ingest command ID |
| `knowledge_topics`, `knowledge_item_topics` | normalized many-to-many topics |
| `knowledge_entities`, `knowledge_item_entities` | normalized entities; `link_status` linked/ambiguous/unlinked/not_applicable, optional `person_id` / `lead_id` |
| `knowledge_fts` | FTS5 over filename, summary, text, topics, entities, project, category (falls back to LIKE if FTS5 is missing) |

`captured_at` is upload time. `event_date` is only set when the model returns
an explicit ISO date and is labeled `llm_inferred`; vague dates are dropped.

**Linking.** A person entity links to `people.id` only when exactly one
person has the same normalized full name (two or more tokens). An
organization entity links to `leads.id` only on a unique `normalized_name`
match. Ambiguous or missing matches stay unlinked metadata. Linking never
creates or merges contacts.

## Retrieval

- `GET /api/knowledge/search?q=&project=&category=&topic=&entity=&person_id=&content_kind=&event_from=&event_to=&captured_from=&captured_to=&date_from=&date_to=&limit=`.
  `date_*` filters on the effective date (`event_date`, else capture date).
- Ranking: FTS5 bm25 with column weights. All terms are tried first, then any
  term. The response reports `engine`, `match_mode`, `score`, and `position`.
- Each hit carries `source` (item ID, filename, original URL, captured/event
  dates), a snippet, and metadata.
- Typed read tool `search_knowledge` (registered in `tools/registry.py`, so
  it is also available to the validated LLM planner).
- `/ask`: questions starting with `knowledge:`, `kb:`, or `search knowledge`,
  or containing phrases such as "in my notes" or "what do my documents say",
  route deterministically to `search_knowledge`. Explicit dates such as
  "in September 2026" or "since 2026-08-15" become effective-date filters.
  With `use_llm`, the model sees only the top retrieved passages. Its answer
  is shown only when every citation `[K<id>]` refers to a provided passage;
  otherwise only the deterministic evidence list is returned. No match is
  reported as "not evidence".

## UI

`/knowledge` has the drop zone, a file-picker fallback, per-file
progress/status, duplicate links, search with filters, metadata "Browse"
facets (virtual folders by project/category/topic), and the inbox.
`/knowledge/items/{id}` shows the extracted text, metadata, topics, entities
with links, and related items (shared topics/entities).

## Configuration

```
KNOWLEDGE_STORAGE_DIR=./knowledge_store
KNOWLEDGE_MAX_UPLOAD_MB=25
KNOWLEDGE_VISION_MODEL=          # empty = vision disabled
KNOWLEDGE_CLASSIFY=true          # false = store extraction only
```

## Tests

```bash
python -m pytest tests/test_knowledge_ingestion.py tests/test_knowledge_search_and_routes.py -v
```

All fixtures (PDF, DOCX, PNG) are generated in memory and use `tmp_path`
databases and storage. The model is replaced with scripted `chat_fn`s.

## Limitations

- SQLite only; PostgreSQL returns `knowledge_postgresql_runtime_unsupported`.
- No embeddings or hybrid retrieval yet; ranking is FTS5 bm25 only.
- Scanned PDFs are stored and flagged `needs_vision`, but not OCRed.
- No re-classification or deletion UI yet; items are immutable after ingest.
- Consolidation and contradiction detection are not implemented.
