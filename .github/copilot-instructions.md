# CommerceGov Contacts coding instructions

Read and follow `/AGENTS.md`. The detailed product, architecture, data model,
tool contracts, safety behavior, and phased acceptance criteria are in `/docs`.

Do not give an LLM unrestricted database access or execute model-generated SQL.
All actions use typed, validated application services and explicit transaction
boundaries. Preserve contact provenance, verification state, auditability, and
isolated tests. The current roadmap phase limits scope.

