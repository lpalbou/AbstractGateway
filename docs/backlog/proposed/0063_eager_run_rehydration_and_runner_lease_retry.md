# Proposed: Eager run rehydration on startup and runner lock-retry fix

## Metadata
- Created: 2026-07-05
- Status: Proposed
- Completed: N/A
- Roadmap phase: 1 (make hosted multi-user durable)
- Effort: S-M

## ADR status
- Governing ADRs: None.

## Context

Gateway promises durable runs. In hosted multi-user mode, two code paths break
that promise across a restart.

## Current code reality (investigated)

- `src/abstractgateway/service.py:345-350`: `start_gateway_runner()` returns
  immediately in multi-user mode ("Per-principal services are created and started
  lazily once auth resolves the current user"). No runner starts on boot.
- `service.py:147-162`: a per-principal `GatewayService` (and its runner thread)
  is created only on that principal's first authenticated request. So after a
  crash/redeploy, every idle tenant's in-flight and scheduled runs stay paused
  until that user happens to hit an endpoint. Scheduled/timer runs can miss their
  windows indefinitely.
- Latent availability bug (found during SSE review): a cached per-principal
  service whose `runner.start()` loses a singleton-lock race and returns without
  starting is never retried — the service object is cached (`service.py:154-155`)
  and subsequent calls return it as-is. In split-runner or multi-worker
  topologies a principal's runs can stall permanently if the lock holder dies.

## Problem or opportunity

"Durable runs" is the core promise; today deploys silently stall work, which is a
trust failure adjacent to data loss.

## What we might want to do

1. On boot (hosted mode), enumerate reserved runtimes with non-terminal or
   scheduled runs and start their runners eagerly, instead of lazy per-request
   start. Bound the fan-out (e.g. stagger, cap concurrent runners) to avoid a
   thundering herd — this couples naturally with 0076 (leasing) and per-principal
   service lifecycle.
2. Make runner start idempotent and self-healing: if a runner is not actually
   ticking (lost lock, dead holder), a later access re-attempts start rather than
   returning a dead cached service.

## Dependency boundary

Mostly Gateway-local; interacts with 0076's lease model if adopted.

## Why

Restores durability across restarts and removes a permanent-stall failure mode.

## Promotion criteria

Promote now for the lock-retry fix (small, purely corrective). Promote eager
rehydration alongside per-principal lifecycle work (0076) or independently.

## Validation to require on promotion

- Test: create runs for two principals, restart the app (fresh process/service
  cache), assert both principals' non-terminal runs resume without a prior
  request; assert a scheduled run fires after restart.
- Test: simulate a lost-lock runner and assert a later access re-establishes
  ticking.
