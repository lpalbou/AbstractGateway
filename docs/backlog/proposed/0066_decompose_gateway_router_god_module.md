# Proposed: Decompose the routes/gateway.py god-module

## Metadata
- Created: 2026-07-05
- Status: Proposed
- Completed: N/A
- Roadmap phase: 2 (velocity and reviewability)
- Effort: M

## ADR status
- Governing ADRs: None. Aligns with the repo's <600-line file guideline.

## Context

Nearly the entire HTTP surface lives in one file, which is the bottleneck for
every other improvement and a systemic authorization-review risk.

## Current code reality (verified)

- `src/abstractgateway/routes/gateway.py` is 22,768 lines and holds 179 of the
  package's 186 endpoints (`app.py` has 5, `routes/triage.py` has 2). It is ~48%
  of the ~47k-line package.
- It spans unrelated domains: user admin, runs/ledger/SSE, voice/image/video/
  music, discovery contracts, bug/feature report inboxes, email, KG queries, and
  the ops routes (processes/backlog/triage).
- The auth-sensitive handlers (artifact/file/export path logic, admin routes)
  are interleaved with everything else, so a new route can silently forget
  principal-scoping or admin gating in a file no reviewer reads end to end.

## Problem or opportunity

Every feature, review, and tool-assisted edit must load a 22k-line context; the
blast radius of any change is the whole API. This compounds monthly.

## What we might want to do

1. Split into ~12 domain routers (<~1,500 lines each): runs, ledger/SSE,
   commands, artifacts, bundles+visualflows, catalog, discovery/contracts, media,
   prompt-cache, admin/users/sessions, kg/embeddings, reports/email — plus a
   shared dependencies module (principal resolution, service lookup, envelope
   helpers).
2. Move the ops routes (processes/backlog/triage) into their own router as part
   of 0067.
3. Pure mechanical, behavior-preserving moves guarded by the existing route
   tests; no contract change.

## Dependency boundary

Gateway-only.

## Why

Unblocks the authorization contract test (0070), the ops extraction (0067), and
all future work; makes the trust surface reviewable.

## Promotion criteria

Promote after 0059/0060/0061 (cheap truth fixes) and before large feature work.

## Validation to require on promotion

- No route path or response body changes; full existing route test suite passes
  unchanged before/after the split.
