# 0929 — Automations v2 gateway side: authenticated `emit_event` admission into the runtime's durable inbox, generic event source exposure, terminal-run source reconciliation hooks

**Status**: planned (next phase after Automations v1) · **Priority**: P2 · **Created**: 2026-09-26
**Package**: abstractgateway · **Related**: abstractgateway 0928 (Automations v1 — prerequisite),
abstractframework backlog 0928 (master item), abstractgateway 0073 (run lifecycle webhooks / event egress)
**Design**: untracked/design/automations-PLAN.md (2026-09-26)

## Why this is here

v1 ships `schedule@1` and `manual@1` trigger sources only. The operator ruled (2026-09-26)
that external triggers are the next phase and are planned, not merely proposed. The design's
v2 scope is: durable external inbox, generic events, terminal chaining/reconciliation,
constrained fetching. The runtime owns the inbox and the `TriggerAdapter` contract; the
gateway's part is authenticated admission and exposure.

## This package's slice

1. **Authenticated admission.** An authenticated route (or `emit_event` command variant)
   that admits an external event for an automation's event-kind binding into the runtime's
   durable inbox — idempotent on a caller-supplied `event_id`, scoped to the principal's own
   store, rejected with the contract F error envelope (`404 automation_not_found`,
   `409 invalid_state`, `422 unknown_trigger_source|invalid_definition`). Acceptance is a
   receipt; the admission outcome is ledger-recorded by the runtime.
2. **Generic event source exposure.** `GET /trigger-sources` lists event-kind sources
   (`capabilities.kind:"event"`) with `available`/`unavailable_reason`, and the create/patch
   routes accept bindings to them; no gateway-side vocabulary edits per source.
3. **Terminal-run source reconciliation hooks.** When a run reaches a terminal state, the
   gateway hands the runtime's terminal-run source what it needs to admit chained
   occurrences, and on startup reconciles terminal runs the source has not yet observed
   (no lost or duplicated admissions across restarts).

## Current code reality

- `POST /commands` already accepts `emit_event` (`routes/gateway.py:28465`), appended to the
  durable command store and applied by the runner; it targets a run, not an automation
  binding, and carries no source/event identity.
- `runner.py:1293` `_poll_commands` advances past a failing command with only a log line —
  admission must record its outcome durably instead.

## Validation

- Tests: authenticated vs unauthenticated admission; duplicate `event_id` admits once;
  cross-principal admission refused; restart between terminal run and admission yields
  exactly one chained occurrence.

## Dependencies

- 0928 released; AbstractRuntime v2 durable inbox and event/terminal-run trigger adapters.
