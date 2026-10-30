# 0981-gateway: [FEATURE] Add isolated host CPU, storage, swap and transfer counters

> Package: abstractgateway
> Type: feature
> Created: 2026-10-30
> Status: planned
> Priority: P2
> Labels: resources, metrics, observability, seat-framework
> Work-item id: abstractgateway-0981
> Owner: framework
> Thread: unavailable (Agora connection refused; local operator request)
> Hub refs: none

## Summary

Add cheap non-blocking host CPU, data-volume storage, swap and disk/network rate
observations to the existing `/host/state` response for Resources. Expose
individual failures and first-sample warm-up instead of fabricated zero metrics.

## Current code reality

`host_metrics.py` provides cached GPU probes, while `host_status_cache.py` shares
expensive probes. `_host_state_payload` runs in the watcher pool, with independent
sections for memory/GPU/models/caches. There is no CPU/disk/swap block. Memory truth
belongs to Core through Runtime and is not reimplemented here. psutil already
backs optional host RAM observation; a missing package must degrade explicitly.

## Why and scope

The accepted design needs useful host metrics beyond memory/GPU. Add a separately
cached, timestamped `system` block to the current snapshot, using psutil and
stdlib disk capacity for the configured data volume. Derive rate metrics from
monotonic counter deltas, report first-sample warm-up and counter reset, isolate
each probe, and collect off the event loop through the existing snapshot path.
Show swap usage without pretending that used RAM or swap implies OS pressure.

Non-goals: privileged probes, temperature/power collectors, OS daemons, duplicated
Core memory logic, new external dependencies, retained history, inference metrics,
automatic resource management. Related: 0980, 0982. ADR state: no change to ADR
0001/0002; observation adds no new control or persistent state.

## Acceptance criteria

- [ ] Timestamped system section exposes CPU percent/load/cores, data-volume capacity, swap used, and interval disk/network rates when supported.
- [ ] Rate/CPU first sample and reset have null values plus reasons; unsupported individual probes cannot invalidate other sections or host state.
- [ ] One shared short-TTL sample services concurrent snapshot callers, on worker threads, with bounded process-local state and no persistence.
- [ ] Tests cover counter delta math, warm-up/reset, partial failures, shared sampling, and additive host-state integration.

## Testing

`python -m pytest tests/test_host_system_metrics.py tests/test_gateway_host_state_endpoints.py tests/test_perf_host_status_cache.py`

## Receipts

- Intake: live source audit, 2026-10-30. Agora unavailable.
