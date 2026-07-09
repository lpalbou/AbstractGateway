# Proposed: Route authorization contract test and exception-swallowing audit

## Metadata
- Created: 2026-07-05
- Status: Proposed
- Completed: N/A
- Roadmap phase: 2 (reviewability and reliability)
- Effort: S (auth test) + M (exception audit)

## ADR status
- Governing ADRs: None.

## Context

With a large route surface and a house style of broad exception handling, both
authorization regressions and silent durability failures are easy to introduce.

## Current code reality (verified)

- Authorization is centralized (`security/authorization.py` route policy table +
  per-handler admin checks) — good — but the surface is a 22,768-line router
  (0066), so a new route can silently omit principal-scoping or admin gating.
  There is no test asserting the invariant across all routes.
- ~153 occurrences of `except Exception:\n pass` in `src/`;
  `routes/gateway.py` alone has ~206 trailing `except Exception:` blocks. Some
  swallow durability-relevant failures, e.g. the runner's command-cursor save
  (`runner.py` ~259-262) and `_repair_terminal_subworkflow_waits`
  (`runner.py` ~295-298, ~367-369).

## Problem or opportunity

The mission is truthful state on a durable substrate; silent authorization gaps
and swallowed persistence errors are the opposite, and they make incident
debugging archaeology.

## What we might want to do

1. Add a contract test that enumerates all routes and asserts: every non-public
   route resolves a principal; every admin-class route is admin-gated (and,
   post-0067, every ops route requires `ops:exec`).
2. Audit the `except Exception: pass` sites: keep intentional best-effort ones
   but log at least a debug/warning with context; for durability-relevant writes
   (cursor save, ledger append, wait repair) surface or retry instead of
   swallowing.

## Dependency boundary

Gateway-only.

## Why

Prevents future isolation regressions and converts silent failures into
diagnosable ones.

## Promotion criteria

Promote the auth contract test with 0066; the exception audit can follow.

## Validation to require on promotion

- The auth contract test fails if a route is added without principal-scoping or
  required gating.
- Durability-relevant except blocks either log with context or propagate; a test
  covers at least the cursor-save and wait-repair paths.
