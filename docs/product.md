# Product Definition

## Purpose

CommerceGov Contacts is a local contact-intelligence assistant. A user pastes
raw text copied from a company website, directory, professional profile, note,
or email. The system extracts organizations, people, contact methods,
relevance signals, and provenance into a controlled contacts database. The
user can then ask natural-language questions and request narrowly defined
actions through the application on port 8025.

The product optimizes for trustworthy contact collection, retrieval,
organization, follow-up planning, and clear communication. It is not a general
autonomous agent and not a full CRM.

## Primary users and environment

- One trusted operator on a local machine.
- FastAPI binds to loopback by default at `127.0.0.1:8025`.
- A local LM Studio or Ollama-compatible model performs extraction and intent
  planning.
- PostgreSQL is the target system of record. SQLite remains only during the
  first hardening phase and may be used for isolated tests.

## Core jobs

1. Capture raw text and its source.
2. Extract structured contact intelligence without losing provenance.
3. Review uncertain or conflicting extraction results.
4. Find and rank relevant organizations and people already in the database.
5. Identify missing, unverified, stale, or incomplete contact information.
6. Calculate transparent analytics from database records.
7. Create and organize follow-up tasks with explicit user control.
8. Maintain an audit trail of commands and mutations.

## Representative commands

- "Show relevant Shopify agencies without a contact person."
- "List high-relevance companies with no open follow-up."
- "Which companies are missing a verified email?"
- "Show contact coverage analytics by relevance tier."
- "Create follow-up tasks for these three approved companies next Monday."

"Find emails for companies that are missing" is intentionally ambiguous. In
the foundation phase it means list companies whose database records lack a
usable email. External discovery becomes a separate, explicitly enabled tool
in a later phase and must preserve source and verification state.

## Success criteria

- Every important fact can be traced to a source or marked as user-entered.
- Contact values have explicit confidence and verification state.
- The same command produces predictable tool selection and filters.
- Failed writes do not partially modify data.
- The user can see what a command read, proposed, changed, or could not do.
- The LLM can be unavailable without corrupting the database.

## Out of scope

- Generic autonomous browsing or scraping.
- Arbitrary SQL agents.
- Email sending, inbox synchronization, or campaign automation.
- Generic workflow engines such as n8n.
- Multi-tenant SaaS, billing, teams, and enterprise permissions.
- A broad CRM feature set unrelated to contact intelligence.

