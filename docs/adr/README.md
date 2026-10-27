# Architectural Decision Records (ADRs)

ADRs record the durable rules AbstractGateway's design follows: *why* it is built this way.
What was built, and when, lives in `docs/backlog/`. An accepted ADR is live policy: code,
tests, reviews and backlog items cite it, and a change that conflicts with it revises the ADR
first or is refused.

## Index

| ID | Title | Status | Date | Summary |
|----|-------|--------|------|---------|
| 0001 | [AbstractCore access goes through AbstractRuntime facades](0001_abstractcore_access_through_runtime_facades.md) | Accepted | 2026-10-27 | The gateway imports AbstractRuntime, never AbstractCore; a missing facade name is a runtime patch, not a gateway import |

## Adding an ADR

1. Create `docs/adr/NNNN_short_title.md` with Status, Context, Decision, Consequences,
   Enforcement, Validation, Backlog links, Related.
2. Add it to the index above and link it from the backlog items it governs.
3. Supersede rather than edit: an accepted ADR changes status to "Superseded by NNNN".
