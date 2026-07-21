# Completed: Route authorization contract test and exception-swallowing audit

## Metadata
- Created: 2026-07-05
- Status: Completed (both halves shipped 2026-07-21; accepted by laurent same
  night — "a route can never ship unprotected", relayed commons c3890)
- Completed: 2026-07-21
- Roadmap phase: 2 (reviewability and reliability)
- Effort: S (auth test — DONE) + M (exception audit durability core — DONE)

## Progress

- 2026-07-21: the S half shipped as
  `tests/test_gateway_route_authorization_contract.py` (7 tests). Three
  pinned layers: (1) BOUNDARY — every served route is under `/api/gateway`
  (behind the security middleware) or on an explicit public allowlist with a
  recorded reason (console/docs/health/triage capability-URLs); (2) DECISION —
  all 137 write routes have an explicit authorization decision (73 admin-gated
  by `GATEWAY_ROUTE_POLICIES`, 64 on a rationale-grouped user-level allowlist;
  both directions pinned so the list stays honest), plus a dead-row check
  (every policy row must match ≥1 live route) and a pin that session/login is
  the ONLY public write; (3) SERVED SURFACE — a real non-admin principal is
  403'd through the real middleware on a representative route of every policy
  row, representative user-level writes pass authz, and unauthenticated
  requests are refused. The entities-only twin
  (`test_gateway_entities_admin_gate.py`) keeps its richer per-route proofs.
- 2026-07-21 (same night): the M half's durability-relevant core shipped. A
  tree-wide probe (silent `except Exception: pass/continue/return` within 25
  lines of a durable write) found 13 sites; all fixed. Loud (`logger.exception`
  / `warning` with run/command context + named consequence): the command-cursor
  save (`runner._poll_commands` — restart replays commands), the terminal-
  subworkflow wait-repair pass, failed promote-to-FAILED after tick exception,
  parent-resume after terminal child, compacted-vars saves (out-of-band +
  auto-compact — a lost save discards paid LLM compaction), auto-compact guard
  save (thrash risk), backlog-exec run/ledger persistence, session-memory
  anchor save (`routes/gateway.py`). Marker-only/best-effort writes keep their
  swallow but log at DEBUG with `exc_info` (schedule/compact observability
  markers, chat-thread dedup fallthrough — which retries via the primary
  path). Pinned by `tests/test_gateway_runner_swallow_audit.py`: both
  backlog-named paths (cursor save, wait repair) must survive the failure AND
  log with context. Remaining `except Exception:` population (~150 sites) is
  diagnostics/close/parse best-effort — not durability-relevant; no further
  blanket churn planned.

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
