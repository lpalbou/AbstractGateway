# 0980-gateway: [IMPROVEMENT] Implement the Resources dashboard with shared chrome and truthful memory topology

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: completed
> Completed: 2026-10-30
> Priority: P1
> Labels: resources, console, ui, memory, seat-framework
> Work-item id: abstractgateway-0980
> Owner: framework
> Thread: unavailable (Agora connection refused; local operator request)
> Hub refs: none

## Summary

Turn the accepted Resources prototype into the real console page. Preserve the
AbstractUIC theme chooser, doc assistant, About and clickable username/status menu
in the reserved header. Adapt memory capacity and chart semantics to unified RAM,
dedicated GPU VRAM, CPU-only hosts, and unknown hardware.

## Why

The operator approved the graphical design, then required the shared chrome and
explicit unified-memory versus RAM+VRAM support. Long explanations currently hide
the resource controls below the fold. Other agents share the checkout: edit and
commit only this task's paths/hunks, never sweep their changes into a commit.

## Current code reality

`console.py` owns `tab-models`, `renderHostState`, the seq-guarded snapshot fetch,
provider-truth residency, cache inventory and admin mutations. `console_ui.py`
mounts the shared `AfTopBarActions`/appearance/account islands. `deviceMeterView`
prefers allocator-held bytes, which is useful on Metal but must not substitute
process allocations for device-wide CUDA total-minus-free VRAM. The prototype at
`design/resources` is simulated and has local chrome; do not ship that chrome.

## Scope

Dashboard, compact Gateway controls, info icons via the existing kit tooltips,
inspectors via kit modal binding, snapshot-backed metric cards, bounded real
browser-local trends labeled with collection scope, series/range/scrub interaction,
loaded/configured/cache distinctions, and existing load/unload/lock/clear behavior.
Memory type uses explicit topology if available, known Metal/MPS or CUDA device
evidence otherwise, and unknown when evidence is insufficient. Never infer from
host platform alone or add RAM and VRAM/heap into a combined capacity total.

Non-goals: replacing shared themes/header, synthetic production metrics, retained
server history, automatic pressure policies, changing model authority, touching
unrelated work, restarting/deploying the operator's gateway.

## Dependencies and ADR state

Related: 0981 (system probes), 0982 (retained telemetry), 0968 (model-host controls),
0978 (pool queue). ADR 0001 and 0002 remain governing; no new durable policy or
at-rest schema is introduced. Shared UI requirements come directly from operator.

## Acceptance criteria

- [x] Existing AbstractUIC theme chooser, doc assistant, About and account/status menu remain functional and correctly positioned.
- [x] Unified memory is one RAM pool with overlapping scoped heap; CUDA device-wide VRAM is independent of host RAM and process allocation.
- [x] CPU-only, unknown, unsupported and zero readings stay distinct; scopes, estimates and failed probes remain clear.
- [x] Real sampled history has timestamps, bounded storage, visible gaps and a "since this page opened" label; no generated history is served.
- [x] Models/caches and Gateway operations retain capability/admin gating, confirmation and verified refresh behavior.
- [x] Desktop/mobile and multiple shared themes render without overflow; tooltips, inspectors and charts work with keyboard navigation.

## Testing

`python -m pytest tests/test_gateway_console.py tests/test_gateway_console_responsive.py tests/test_gateway_console_gateway_card.py tests/test_gateway_console_topbar_resources_r10.py tests/test_console_resources_dashboard.py`

`ABSTRACTGATEWAY_BROWSER_TESTS=1 python -m pytest tests/test_gateway_console_browser_resources.py tests/test_gateway_console_browser_r10w2.py`

Browser fixtures must use scratch state and route-controlled hardware snapshots;
never query providers or mutate the operator's models/configuration for validation.

## Receipts

- Intake: source audit and operator correction, 2026-10-30. Agora unavailable.

## Completion report

Completed: 2026-10-30 (unreleased). Implementation receipt: `ee918dc9`.

Graphical Resources content integrated with existing AbstractUIC chrome, tooltip and modal binding; scoped unified/VRAM capacity; real bounded receipt-time history; existing operations and read-only gates.

Validation: 124 pytest checks passed, with the two previously failing Runtime-gated checks explicitly deselected after reproducing both at untouched baseline `0bfe34d5`. The full rendered-console browser lane passed **96 checks** across light/dark themes and five widths.

Evidence: [integration report and screenshots](../../../../design/resources/IMPLEMENTATION.md), [machine-readable validation](../../../../design/resources/integration-validation.json), `tests/test_gateway_console_browser_resources.py`, `tests/browser/resources_dashboard.mjs`, `tests/test_console_resources_dashboard.py`, `tests/test_host_system_metrics.py`, and the additive host-state regression in `tests/test_gateway_host_state_endpoints.py`.

Final checks included console parsing/login/default-routing, responsive and shared-header contracts, existing model routes, host snapshot/cache/pause logic and import boundaries. The two Runtime failures (`test_gateway_model_residency_capability_contract_comes_from_runtime_host_facade`, `test_pause_resume_routes_and_health_flag`) arise from installed Runtime 0.15.2 metadata against Gateway's existing 0.15.3 gate; no Runtime/version/dependency changes were made.

ADR impact: none; ADR 0001/0002 boundaries and existing host authorization are preserved. Follow-ups: proposed 0982 covers retained telemetry, serving/pressure/thermal metrics and explicit topology provenance for additional hardware. Browser-local history is intentionally not collection while the page is closed. Agora remained unavailable (connection refused): no hub claim/mirror or receipt could be minted; this file, git commit and validation artifacts carry the durable receipt.

Original path: `planned/resources/0980_resources_dashboard_and_memory_topology.md`. Final path: `completed/resources/0980_resources_dashboard_and_memory_topology.md`. Other agents' changes and the running Gateway were left untouched.

Additional topology receipt: `dfc72e6f` requires same-host Apple Silicon evidence for MPS; Intel/unknown MPS and remote hardware stay unknown. The post-hardening focused lane passed **37 tests**, including the 96-check browser lane and all new topology/sampler/host-state tests.
