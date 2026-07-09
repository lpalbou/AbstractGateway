# Proposed: Run-lifecycle webhooks and signed event egress

## Metadata
- Created: 2026-07-05
- Status: Proposed
- Completed: N/A
- Roadmap phase: 3 (adoptability)
- Effort: S-M

## ADR status
- Governing ADRs: None.

## Context

A control plane that can only be polled is half a control plane. There is
currently no way for an external system to be told a run finished or is waiting.

## Current code reality (verified)

- The only outbound notifications are operator-facing Telegram/email
  (`maintenance/notifier.py`) and a hypothetical webhook comment
  (`runner.py:169`). No general server-to-server egress exists.
- Clients must open an SSE stream or poll the ledger to learn about terminal
  states and waits.

## Problem or opportunity

Backends, CI, and automation platforms integrate via callbacks, not long-lived
SSE. This is the cheapest expansion of who can use the gateway.

## What we might want to do

1. Register per-run or per-workflow webhooks that fire on terminal states and
   waits (approval/JOB), with signed payloads (HMAC), retry with backoff, and a
   dead-letter path.
2. Reuse the durable ledger as the source of truth so delivery is at-least-once
   with a durable cursor (interlocks with 0075) and survives restarts.

## Dependency boundary

Gateway-owned; consumes ledger tail/notify (0075) for efficient triggering.

## Why

Enables automation/integration use cases the current UI-only surface cannot
serve, without the adopter running an SSE client.

## Promotion criteria

Promote after event-driven streaming (0075) so triggering is efficient, or with a
simple poll-based trigger initially.

## Validation to require on promotion

- Test: a run reaching terminal/wait fires a signed webhook; failures retry and
  land in the dead-letter after N attempts; delivery survives a restart.
