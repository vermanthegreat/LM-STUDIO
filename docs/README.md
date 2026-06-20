# CommerceGov Contacts Specification

This directory is the build contract for the local contact-intelligence
assistant.

| Document | Authority |
|---|---|
| `product.md` | Purpose, users, jobs, success criteria, and excluded scope |
| `architecture.md` | Components, request flow, trust boundaries, and configuration |
| `data-model.md` | Target PostgreSQL entities and integrity rules |
| `tool-contracts.md` | Allowed agent tools, risk classes, and result envelopes |
| `safety-and-communication.md` | Approval, privacy, injection, failure, and response rules |
| `implementation-roadmap.md` | Phase order, acceptance criteria, and stop conditions |
| `agent-system-prompt.md` | ChatGPT/local-model runtime behavior prompt |
| `first-patch-prompt.md` | Executable prompt for the immediate hardening patch |

Repository coding agents must start with `/AGENTS.md`. Cursor automatically
loads `/.cursor/rules/commercegov-contacts.mdc`; GitHub-compatible assistants
can load `/.github/copilot-instructions.md`. All platform-specific instructions
defer to these shared specifications.

Security is enforced by application code, schemas, transactions, and database
constraints. Model prompts reinforce behavior but are never treated as the
security boundary.

