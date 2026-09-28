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
- Semantic retrieval (Phase K1) is opt-in; see below for its own limitations.
- Scanned PDFs are stored and flagged `needs_vision`, but not OCRed.
- No re-classification or deletion UI yet; items are immutable after ingest.
- Consolidation and contradiction detection are not implemented.

## Phase K1: local embeddings and hybrid retrieval

K1 adds optional semantic search over K0 items. It finds a document about an
"automobile fleet" for the query "car" even though no word overlaps.
Embeddings are **disabled by default**, and everything above keeps working
without them.

### Search modes

`GET /api/knowledge/search?...&mode=auto|lexical|semantic|hybrid`, the
`search_knowledge` tool, and the mode selector on `/knowledge` all accept a
mode.

| Mode | Behavior |
|---|---|
| `lexical` | K0 FTS5 bm25 (all terms, then any term). Unchanged. |
| `semantic` | Cosine similarity between the query embedding and chunk embeddings; one result per item (its best chunk); scores below `KNOWLEDGE_SEMANTIC_MIN_SCORE` are dropped. |
| `hybrid` | Reciprocal Rank Fusion of lexical and semantic rankings: `rrf = Σ 1/(60 + rank)`, one result per item. |
| `auto` (default) | `hybrid` when embeddings are enabled, otherwise `lexical`. |

The ordering is deterministic. Semantic results sort by score descending,
then item id, then chunk index. Hybrid results sort by RRF score descending,
then best single-list rank, then item id.

Every response reports `mode_requested`, `mode_used` and a `semantic` block
(`status`: `not_requested`, `disabled`, `no_query`, `no_index`, `ok` or
`error`, plus `error_code`). Each hit carries `retrieval.evidence`
(`lexical`, `semantic` or both), its per-list ranks, and, when semantic
evidence exists, `retrieval.chunk` (`chunk_id`, `chunk_index`, character
offsets, text) and `source.chunk_id`. Citations stay item-level (`[K<id>]`).

### Fallback

If embeddings are disabled, nothing is indexed for the configured model, the
provider errors or times out, or the query vector's dimension does not match
the index, the request returns lexical results with
`mode_used = "lexical"` and the reason in `semantic`. This happens after one
provider attempt per request, with no retry within the request, and it is
logged as a warning. Semantic results are never invented, and `/ask`
evidence only ever contains the retrieved chunks.

### Configuration

```
KNOWLEDGE_EMBEDDINGS_ENABLED=false     # opt in
KNOWLEDGE_EMBEDDING_MODEL=             # required when enabled, e.g. an embedding model loaded in LM Studio
KNOWLEDGE_EMBEDDING_BASE_URL=          # default: LMSTUDIO_BASE_URL (POST <base>/embeddings)
KNOWLEDGE_EMBEDDING_TIMEOUT=30
KNOWLEDGE_EMBEDDING_BATCH_SIZE=16
KNOWLEDGE_EMBEDDING_ALLOW_REMOTE=false # non-loopback endpoints are refused unless true
KNOWLEDGE_EMBED_ON_INGEST=true         # embed new uploads right after they are stored
KNOWLEDGE_SEMANTIC_MIN_SCORE=0.25      # cosine threshold; model-dependent
KNOWLEDGE_SEMANTIC_MAX_CANDIDATES=5000 # max chunk vectors scored per query
```

**Choosing a model.** Load an embedding model in LM Studio (one that
appears as an embedding model and is served at `/v1/embeddings`), then set
`KNOWLEDGE_EMBEDDING_MODEL` to its identifier as shown by
`curl http://localhost:1234/v1/models`. Nothing is downloaded by this app.
Tune `KNOWLEDGE_SEMANTIC_MIN_SCORE` for your model: too low adds weak
matches, too high hides real ones.

**Privacy.** Chunk text and queries go only to the configured endpoint, which
must be loopback (`localhost`, `127.0.0.1`, `::1`) unless
`KNOWLEDGE_EMBEDDING_ALLOW_REMOTE=true`. Otherwise the runtime reports
`embedding_remote_endpoint_not_allowed` and stays lexical. Tool results
written to the command log contain chunk references, not chunk text.

### Storage

Two tables are added by `db.init_db`. The migration is additive; existing K0
rows are untouched.

- `knowledge_chunks`: deterministic paragraph chunks of an item's extracted
  text (about 1000 characters, at most 1500), with `(item_id, chunk_index)`
  unique and a SHA-256 `fingerprint` of the chunker version plus the text.
- `knowledge_chunk_embeddings`: one row per `(chunk_id, model)` holding the
  `dimension`, the L2-normalized float32 `vector`, the `fingerprint` it was
  computed from, `status` (`ok`/`failed`), `error_code`, `attempts` and
  `indexed_at`.

A vector is used only when its status is `ok`, its model is the configured
model, its fingerprint equals the chunk's current fingerprint, and its
dimension equals the model's established dimension (set by the first stored
vector). So:

- **Unchanged chunks** are skipped on re-index.
- **Changed text** makes the old vector stale; it is re-embedded in place,
  keeping the same chunk id.
- **Changing the model** leaves old vectors ignored (counted as
  `other_model_embeddings`) until you re-index. Until then searches fall back
  to lexical with `semantic.status = "no_index"`.
- **A vector of a different dimension** for the same model is stored as
  failed with `embedding_dimension_mismatch`, and a mismatched query vector
  falls back to lexical.

### Indexing existing K0 knowledge

New uploads are embedded after they are committed (if
`KNOWLEDGE_EMBED_ON_INGEST=true`). An embedding failure never fails or
removes the upload. Existing items are indexed on demand, never at startup:

```bash
# status (optionally ?item_id=N)
curl http://127.0.0.1:8025/api/knowledge/embeddings/status
# one bounded pass (limit 1-2000, default 200); repeat until eligible == 0
curl -X POST http://127.0.0.1:8025/knowledge/embeddings/reindex \
     -H 'content-type: application/json' -d '{"limit": 500}'
# retry chunks that already failed 3 times
curl -X POST http://127.0.0.1:8025/knowledge/embeddings/reindex \
     -H 'content-type: application/json' -d '{"retry_failed": true}'
```

Each pass reports `eligible`, `indexed`, `skipped_unchanged`, `failed`,
`skipped_max_attempts`, `deferred` and `error_codes`, and is recorded in the
command log as `knowledge_reindex`. Failure handling:

- A bad input fails only its own chunk; the batch is retried one chunk at a
  time.
- A provider outage (timeout, unreachable, HTTP error) stops the pass after
  one batch.
- Failed chunks are retried on later passes up to 3 attempts, then only with
  `retry_failed`.

With embeddings disabled the endpoint returns 409 and changes nothing.

The `/knowledge` page shows the embedded/total chunk counts, failures, and
chunks awaiting re-index.

### Tests and local verification

```bash
python -m pytest tests/test_knowledge_k1_embeddings.py -q
python -m pytest tests/test_knowledge_ingestion.py tests/test_knowledge_search_and_routes.py -q
python -m pytest tests/test_query_counts.py -q
python -m pytest tests/ -q
```

The tests use a deterministic concept-bucket fake embedder and `httpx`
mock transports. They need no LM Studio, network, model download or real
data, and they assert that the default `leads.db` is not modified.

For a live check, start LM Studio with an embedding model loaded, set the
variables above, run `python app.py`, POST a re-index, then search with
`mode=semantic` for a paraphrase of a stored document.

### K1 limitations

- Similarity is computed in Python over at most
  `KNOWLEDGE_SEMANTIC_MAX_CANDIDATES` vectors per query (response flag
  `semantic.truncated`). This fits local scale, not large corpora.
- Only extracted text is chunked; items without text (images without vision,
  scanned PDFs) have no embeddings.
- The minimum score is a single global threshold and must be tuned per model.
- SQLite only; no approximate-nearest-neighbour index.
