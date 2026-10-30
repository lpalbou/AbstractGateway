# 0981-gateway: [FEATURE] Add isolated host CPU, storage, swap and transfer counters

> Package: abstractgateway
> Type: feature
> Created: 2026-10-30
> Status: completed
> Completed: 2026-10-30
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

- [x] Timestamped system section exposes CPU percent/load/cores, data-volume capacity, swap used, and interval disk/network rates when supported.
- [x] Rate/CPU first sample and reset have null values plus reasons; unsupported individual probes cannot invalidate other sections or host state.
- [x] One shared short-TTL sample services concurrent snapshot callers, on worker threads, with bounded process-local state and no persistence.
- [x] Tests cover counter delta math, warm-up/reset, partial failures, shared sampling, and additive host-state integration.

## Testing

`python -m pytest tests/test_host_system_metrics.py tests/test_gateway_host_state_endpoints.py tests/test_perf_host_status_cache.py`

## Receipts

- Intake: live source audit, 2026-10-30. Agora unavailable.

## Completion report

Completed: 2026-10-30 (unreleased). Implementation receipt: `ee918dc9`.

Shared worker-side CPU/storage/swap/disk/network probes added to host state. Warm-up, counter resets and per-probe failures are explicit; optional psutil absence preserves disk capacity. No extra threads, services or persistent state.

Validation: 124 pytest checks passed, with the two previously failing Runtime-gated checks explicitly deselected after reproducing both at untouched baseline `0bfe34d5`. The full rendered-console browser lane passed **96 checks** across light/dark themes and five widths.

Evidence: [integration report and screenshots](../../../../design/resources/IMPLEMENTATION.md), [machine-readable validation](../../../../design/resources/integration-validation.json), `tests/test_gateway_console_browser_resources.py`, `tests/browser/resources_dashboard.mjs`, `tests/test_console_resources_dashboard.py`, `tests/test_host_system_metrics.py`, and the additive host-state regression in `tests/test_gateway_host_state_endpoints.py`.

Final checks included console parsing/login/default-routing, responsive and shared-header contracts, existing model routes, host snapshot/cache/pause logic and import boundaries. The two Runtime failures (`test_gateway_model_residency_capability_contract_comes_from_runtime_host_facade`, `test_pause_resume_routes_and_health_flag`) arise from installed Runtime 0.15.2 metadata against Gateway's existing 0.15.3 gate; no Runtime/version/dependency changes were made.

ADR impact: none; ADR 0001/0002 boundaries and existing host authorization are preserved. Follow-ups: proposed 0982 covers retained telemetry, serving/pressure/thermal metrics and explicit topology provenance for additional hardware. Browser-local history is intentionally not collection while the page is closed. Agora remained unavailable (connection refused): no hub claim/mirror or receipt could be minted; this file, git commit and validation artifacts carry the durable receipt.

Original path: `planned/resources/0981_host_cpu_storage_and_swap_metrics.md`. Final path: `completed/resources/0981_host_cpu_storage_and_swap_metrics.md`. Other agents' changes and the running Gateway were left untouched.

Additional topology receipt: `dfc72e6f` requires same-host Apple Silicon evidence for MPS; Intel/unknown MPS and remote hardware stay unknown. The post-hardening focused lane passed **37 tests**, including the 96-check browser lane and all new topology/sampler/host-state tests.
