# Proposed: Failure-mode test suite (concurrency, crash, corruption, backpressure)

## Metadata
- Created: 2026-07-05
- Status: Proposed
- Completed: N/A
- Roadmap phase: 2 (reliability)
- Effort: M

## ADR status
- Governing ADRs: None.

## Context

The suite is voluminous and behavioral (344 tests in 80 files, TestClient + real
runner + real bundles — a genuinely good shape), but it validates the failure
modes the product sells only on happy paths.

## Current code reality (verified)

- The flagship contract test (`test_capabilities_endpoint_contract.py` ~96-116)
  asserts hardcoded endpoint strings equal the same hardcoded strings — a change
  detector, not a truthfulness check.
- No tests found for: concurrent command application to one run, SSE under many
  clients / backpressure, crash-recovery mid-tick (kill + restart), ledger
  corruption injection, multi-worker uvicorn, or auth lockout under parallel
  failures.
- `conftest.py` (~39) must pop `abstractgateway.app` from `sys.modules` between
  every test because of the module-level service singleton — the singleton design
  leaks into test architecture.

## Problem or opportunity

Durability and isolation are the core promises; they are asserted in docs and
validated only on sunny days.

## What we might want to do

1. Concurrency: parallel/duplicate commands to one run (idempotency by
   `command_id`), many SSE clients, backpressure/slow-client behavior.
2. Crash-recovery: kill the runner mid-tick, restart, assert non-terminal runs
   resume and `_repair_terminal_subworkflow_waits` actually repairs (ties to
   0063).
3. Corruption injection: truncated/concatenated JSONL lines are recovered per the
   documented `_decode_line` behavior (and verify the AGENTS.md lock+fsync claim
   against the actual shipped Runtime store — a referee flagged it may be false).
4. Multi-worker smoke test once 0076 leasing exists.

## Dependency boundary

Gateway-owned tests; crash/corruption tests exercise the Runtime store behavior.

## Why

Converts the durability/isolation promises from documentation into verified
properties, and replaces the tautological contract test with a behavioral one.

## Promotion criteria

Promote alongside 0063/0075 (so the recovery/streaming paths under test are the
new designs) or incrementally now for the existing paths.

## Validation to require on promotion

- New tests fail on injected concurrency/crash/corruption regressions and pass on
  the current+fixed behavior; the capabilities test asserts a behavioral property,
  not a string echo.
