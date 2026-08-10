# 0233 — Real run cancellation: abort in-flight LLM calls (abstractgateway slice)

**Status**: planned · **Priority**: P1 · **Created**: 2026-08-02
**Master item**: `docs/backlog/planned/0233_real_run_cancellation_abort_in_flight_llm_calls.md`
(framework root) — read it first; it carries the full evidence and the cross-package plan.

## Why this is here
`/cancel` today only writes `status=CANCELLED`; the in-flight generation runs to completion and
keeps consuming GPU/paid tokens. CONFIRMED 2026-08-02 by tracing abstractcode-tui →
`runner.py:1408` `_apply_run_control` → `abstractruntime/core/runtime.py:1302`, plus LM Studio
slot-span analysis (5 overlapping generation pairs, largest 211s).

## This package's slice
- `_apply_run_control` triggers the runtime's cancellation token synchronously and reports
  whether the in-flight call was actually aborted, so clients can distinguish
  "marked cancelled" from "stopped".
- Depends on abstractcore (HTTP abort) and abstractruntime (token threading) landing first.

## Validation
Cancel a long local generation mid-flight; LM Studio must show it stopping within seconds, no slot
left busy, ledger shows `cancelled_in_flight`, and cancelling run A must not disturb run B.
