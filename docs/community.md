# Community Intelligence (Shopify Community / Discourse)

Operator-provided captures of Discourse topics are parsed deterministically and
stored in SQLite. **Nothing is fetched from the network**, no LLM is involved,
and no lead, person, interaction or task is created or linked.

**Compatibility status (M1 BLOCKED):** implemented and tested only against SYNTHETIC fixtures
(`tests/fixtures/community/README.md`). Compatibility with live Shopify
Community output is UNVERIFIED until a genuine capture is added and tested.

## Entry point

`/community` (paste form, topic list) and `POST /community/ingest`
(`raw_text`, `source_url`). `source_url` is required and must be a Discourse topic
URL (`https://<host>/t/<slug>/<topic_id>`); its topic id must match the capture.
The runtime is SQLite-only; PostgreSQL fails closed with
`community_postgresql_runtime_unsupported`.

## Supported capture formats

1. **Discourse topic JSON** (`post_stream.posts[]`): reads `id`, `post_number`,
   `username`, `created_at`, `updated_at`, `reply_to_post_number`, `raw` (preferred)
   or `cooked`, plus topic `id`, `title`, `slug`, `category_id`, `posts_count`,
   `details.created_by.username`.
2. **Rendered text** (LM Studio capture format, not native page copy):

```text
Topic: <title>            (optional)
Category: <name>          (optional)

#<post_number> @<username> <ISO-8601 timestamp with zone> [reply-to #<n>] [edited <timestamp>]
<body lines until the next header>
```

Post URLs are constructed as `<topic URL>/<post_number>` from the operator-supplied
topic URL (Discourse convention). Author profile URLs are not stored (not in the source).

## Identity and deduplication

- Topic: `(forum_host, topic_id)`. Post: `(topic, post_number)`; `post_id` is stored
  when present and conflicting ids/authors are rejected, never overwritten.
- Duplicate post numbers with different content, or one post id on two numbers,
  are rejected as parse errors.
- An identical capture (same topic + SHA-256 of content) is a no-op.
- Body comparison uses whitespace-normalized text (HTML converted to text). A change
  of body *format* between captures is reported as a warning, not an edit.
- Edits store the superseded body in `community_post_revisions`.
- Posts missing from a later capture are never deleted; an incomplete capture is flagged.
- One capture = one transaction; any failure rolls everything back.

## Tables

`community_captures` (original payload + provenance), `community_topics`,
`community_authors`, `community_posts`, `community_post_revisions`.
Created idempotently by `db.init_db` (same pattern as Gmail/knowledge tables).

## Community Intelligence workflow and boundaries (M0)

Goal: help the operator keep track of Shopify Community conversations that matter
to them, using only material the operator saved themselves.

**In scope now:** parsing operator-provided topic captures (above) and
operator-provided listing/search captures (below); local SQLite storage with
provenance; a persistent work queue of topics to ingest.

**Not authorized (prohibited by `AGENTS.md` and `product.md`):** any automated
fetching of Community pages or APIs, browser automation or driving the operator's
logged-in browser, scraping, posting or replying, MCP exposure, and LLM-driven
navigation. Shopify's terms for automated access to the Community have **not**
been verified. Automated discovery may be added only after a later specification
records (a) the evidence that the access is permitted and (b) a fixed, typed
fetcher design; the discovery data model below is source-agnostic so a future
authorized fetcher can feed `record_discovery` unchanged.

The workflow is operator-in-the-loop: the operator opens a listing/search/topic
page, saves the JSON, and pastes it. Discovery never writes conversation content;
topics are ingested one at a time by the existing ingest path.

## Discovery (M2)

Entry points: `/community` (form + "Waiting to ingest" queue) and
`POST /community/discover` (`raw_text`, `source_url` = the listing/search page URL).

Supported JSON shapes (Discourse API shapes; **live Shopify output unverified**):

- listing: `topic_list.topics[]` (`/latest.json`, category listings)
- search: top-level `topics[]` together with `posts[]` or `grouped_search_result`

Read per topic: `id` (required), `slug`, `title`, `category_id`, `posts_count`,
`last_posted_at`, `pinned`. Bad rows are reported, never repaired.

Tables (created idempotently with the other Community tables):
`community_discovery_runs` (original payload + provenance, unique per
source/content hash), `community_discovered_topics` (unique `(forum_host,
topic_id)`), `community_discovery_sightings` (per-run provenance and
classification), `community_discovery_checkpoints` (per host + source key).

Behavior:

- Each sighting is classified `new`, `pending` (seen before, not ingested),
  `ingested`, or `update_available` (stored post count < `posts_count` seen).
  `posts_count` may include posts the capture never shows (hidden/whispers), so a
  topic can stay `update_available`; the operator decides, nothing loops.
- Source key = kind + path + sorted query, ignoring `page` and `.json`, so all pages
  of one listing share one checkpoint.
- Checkpoint = highest `last_posted_at` seen for the source. A capture reports
  `reached_checkpoint` when a non-pinned topic is at or below the previous mark,
  meaning older pages need not be pasted. Pinned topics are ignored for this.
- Post counts and dates seen only move forward.
- Identical capture (same source, same hash) is a no-op. One capture = one
  transaction; failure commits nothing.
- The work queue is derived from stored state, so it survives restarts and shrinks
  as topics are ingested; there is no separate job state to corrupt.
- Each recorded capture writes a counts-only command-log entry.
- The ingest and discovery paths share the SQLite-only runtime restriction.

## User summary ingestion and topic status

`POST /community/discover` also accepts a Discourse **user summary**
(`/u/<username>/summary.json`, top-level `user_summary`). The source URL must be the
`/u/<username>/summary` page so the user is identified (a mismatch with `users[]` is a
warning). **Status: implemented against the documented Discourse shape and a SYNTHETIC
fixture only. Validation against the operator's real CommerceGov summary is PENDING**
(it was not available when this was implemented); the "13 topics" expectation is
unverified.

Extracted: topic ids (union of `user_summary.topic_ids`, `topics[]` and
`replies[].topic_id`, sorted by id), titles/slugs/post counts where `topics[]` has them
(never invented), reply references (`topic_id` + `post_number`, stored in
`community_user_replies`, unique per user/topic/post) and the numeric activity
statistics present (`community_user_activity`, latest values). Topics reuse the
discovery tables and identity `(forum_host, topic_id)`; replies reuse the post identity
`(topic, post_number)`. A changed summary adds only new references.

Topic status (`topic_statuses`, shown on `/community`), deterministic from stored data:

| Status | Meaning |
|---|---|
| `pending` | discovered, nothing ingested |
| `partial` | ingested, but fewer posts stored than reported, or a referenced reply post is not stored |
| `complete` | no known gap |
| `coverage_unknown` | ingested, source gave no post count; absence of a gap is not proof |

The work queue is `pending` + `partial`, ordered as before; `next_topic` returns the
first item. It is derived from the database, so it resumes after a restart and a
re-ingested identical capture changes nothing. No browser or network access exists;
the operator captures each topic and pastes it into the ingest form.
