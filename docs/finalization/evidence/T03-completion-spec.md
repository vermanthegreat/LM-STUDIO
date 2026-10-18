# T03 evidence — narrow SQLite completion specification

- **Task:** T03 (M0). **Owner:** CLAUDE_CLOUD. **Depends on:** T01 (operator: GO).
- **Deliverable:** `docs/completion-spec.md` (new), plus pointer/amendment edits in
  `docs/README.md`, `docs/product.md`, `docs/safety-and-communication.md`,
  `docs/implementation-roadmap.md` (G2 out-of-scope + invariant amendment, new Phase F entry).
- **Tests:** none (documentation only, per backlog).

## Before/after policy table

See `docs/completion-spec.md` section 2. Summary of conflicts resolved:

| Conflict | Resolution |
|---|---|
| Community "no MCP exposure / no LLM / no CRM link" vs. intended stored-data features | Stored-data reads, versioned classification and reviewed CRM links allowed independently of collection |
| G2 "bodies never in prompts" vs. existing local classifier and planned draft assistance | Bounded excerpts to configured local model only |
| G2 "AI drafting out of scope" | Suggestion/draft/approval/attempt/sent states specified (section 7) |
| product/safety "email sending not supported" | Only G2's operator send of an approved snapshot; not implemented yet |
| Marker → task | Proposal + approval only (section 8) |

Unchanged prohibitions: automated Community collection (**NOT AUTHORIZED**), autonomous
send, Calendar, campaigns, generic search/scraping, PostgreSQL acceptance.

## Trace review

Every E2E stage in `E-acceptance.md` "Workflow coverage matrix" maps to a spec section,
task and gate in `docs/completion-spec.md` section 10. No requested capability retains
contradictory authority rules in `docs/`.

## Outstanding items

1. **`AGENTS.md` pointer — BLOCKED (needs HUMAN).** The backlog lists `AGENTS.md`. My edit
   adding a pointer to `completion-spec.md` under "Non-negotiable boundaries" was refused by
   the cloud session's safety classifier, because it widens agent authority in the instruction
   file. Proposed text for the operator to apply or approve:

   ```text
     `docs/completion-spec.md` is that specification for the finalization scope:
     stored-data Community reads/classification, a read-only stdio MCP adapter,
     governed candidate promotion, local-model draft suggestions, email follow-up
     proposals, and operator-approved sending as defined in roadmap Phase G2.
     Automated Community collection remains NOT AUTHORIZED.
   ```

   (Insert after "...unless a later specification explicitly authorizes it.")
2. **`docs/community.md` amendment** is applied at T07, when the Community branch is merged,
   per completion-spec section 2 (that file is not on `main`).
3. **HUMAN scope acknowledgement — PENDING** (completion-spec section 10).

## Status

**PASS (documentation)**, with the HUMAN acknowledgement and the `AGENTS.md` pointer
outstanding. Neither blocks T04/T05; both must be recorded before T06 promotion.
