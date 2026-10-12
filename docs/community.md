# Community conversation capture (Shopify Community / Discourse)

Operator-provided captures of Discourse topics are parsed deterministically and
stored in SQLite. **Nothing is fetched from the network**, no LLM is involved,
and no lead, person, interaction or task is created or linked.

**Compatibility status:** implemented and tested only against SYNTHETIC fixtures
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
