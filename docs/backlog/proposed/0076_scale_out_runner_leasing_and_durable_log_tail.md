# Proposed: Scale-out runner leasing and durable-log tail store

## Metadata
- Created: 2026-07-05
- Status: Proposed
- Completed: N/A
- Roadmap phase: 4 (true scale-out; deferred behind triggers)
- Effort: L

## ADR status
- Governing ADRs: None. Should produce an ADR on the execution/store topology for
  multi-node.

## Context

"Premium control plane" implies more than one node can run. Today the runner is
structurally single-runner, and per-principal services multiply the whole stack.

## Current code reality (verified)

- `runner.py` `GatewayRunnerConfig` (~67-73): 0.25s poll, 4 tick workers, run
  scan limit 200. A `fcntl` singleton lock (~197-218) prevents double-ticking, so
  the design is inherently single-runner per data dir.
- Blocking store I/O runs on the event loop in many `async` handlers (few
  `asyncio.to_thread` uses), so throughput is capped and event-loop stalls
  degrade all endpoints under load.
- Multi-user caches one `GatewayService` per principal, each with its own runner
  thread + `ThreadPoolExecutor(4)` + stores + bundle host (`service.py`), so user
  count is the scaling ceiling: 50 users ~= 50 poll threads + 200 executor
  threads + 50 bundle registries, all scanning disk 4x/sec.
- Runs are per-user-logical (store/data-dir identified), not tied to a specific
  process — which is what makes claim-based leasing feasible.

## Problem or opportunity

The single-node ceiling makes the premium positioning untestable, and the
per-principal multiplication makes hosted mode expensive.

## What we might want to do

1. Durable-log tail store (builds on 0075): monotonic per-run seq and
   `read_after` become the primitive; SQLite is the pragmatic default,
   JSONL deprecated for multi-node.
2. Claim-based run leasing: replace the `fcntl` singleton with atomic run claims
   (`UPDATE ... WHERE status='ready' AND lease IS NULL`, or SKIP LOCKED on
   Postgres), lease renewal + expiry for crash recovery, and idempotent/fenced
   ticks so a slow-but-alive worker cannot double-execute.
3. Shared, bounded executor across principals + idle eviction of per-principal
   services, replacing scan-everything polling with reactive wakeups (0075).
4. Cross-process notify: indexed tail polling by default (zero-dep), Postgres
   `LISTEN/NOTIFY` when a Postgres store backend is adopted.

## Dependency boundary

Substantial AbstractRuntime store-contract and execution cooperation; Gateway
owns the leasing/scheduling policy and the per-principal lifecycle.

## Why

Turns "premium control plane" into a testable claim and removes the per-user cost
multiplication.

## Promotion criteria (explicit triggers — do NOT build prematurely)

Promote when at least one is true and measured:
- sustained concurrent runs or SSE clients exceed what a tuned single node
  handles within target latency;
- a deployment needs >1 worker/pod for availability;
- per-principal thread/FD/IO counts approach host limits.
Until then, prefer 0075 (single-node) + 0063 (rehydration) + 0064 (quotas).

## Validation to require on promotion

- Two runners against one store execute each ready run exactly once under lease
  contention; a killed lease-holder's runs recover after lease expiry.
- Load test demonstrates linear-ish throughput with added runners.
