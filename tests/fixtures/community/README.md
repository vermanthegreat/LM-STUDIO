# Community fixtures: SYNTHETIC

Every file here is hand-written for automated tests. **None is a genuine
Shopify Community capture.** Host `forum.example.invalid` is deliberately fake.
Compatibility with live Shopify Community is UNVERIFIED until a genuine
operator capture is added and tested.

- `synthetic_topic_v1.json` - Discourse-shaped topic JSON, 4 posts (topic 90001).
- `synthetic_topic_v2.json` - same topic later: post #3 edited, post #5 added.
- `synthetic_topic.txt` - rendered-text capture format (see docs/community.md).
- `synthetic_listing_p1.json`, `synthetic_listing_p2.json` - two pages of one Discourse topic listing
  (page 1 has a pinned topic and topic 90001, which matches the topic fixtures above).
- `synthetic_listing_later.json` - the same listing at a later time (one new topic, one topic grown).
- `synthetic_search.json` - Discourse search-result shape (`topics` + `posts`).
- `synthetic_user_summary.json` - hand-written from the documented Discourse `user_summary`
  shape (13 topics, 4 reply references). **Not** the operator's real CommerceGov summary,
  which was not available when this was implemented; real-summary validation is PENDING.
