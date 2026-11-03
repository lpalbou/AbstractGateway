# 0983-gateway: [IMPROVEMENT] Replace the verbose memory inspector with a compact visual summary

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: completed
> Completed: 2026-10-30
> Priority: P1
> Labels: resources, console, ux, seat-framework
> Work-item id: abstractgateway-0983
> Owner: framework
> Thread: unavailable (Agora connection refused; direct operator request)
> Hub refs: none

## Summary

The operator's live screenshot shows that 0980 moved the old Memory & GPU panel
into a modal without simplifying it enough. Replace that modal's content with
two capacity/allocation cards, a compact allocation row and info-icon details.
Remove repeated RAM/heap counters, GPU utilization, host facts and explanatory
paragraphs from the visible memory dialog. Preserve the shared kit shell/themes.

## Current code reality

`console_resources.py:initResourcesDashboard` moves `models-meters`,
`models-breakdown` and `models-host-facts` into `res-memory-legacy`.
`resourcesOpenInspector` exposes the unchanged legacy counters and prose.
`renderResourcesDashboard` already has topology-aware live data and the shared
`bindAccountModal`/`data-af-tip` primitives. No API or probe changes are needed.

## Scope and boundaries

Only the memory inspector's content, width, typography and responsive layout;
known/zero/unknown/partial values; unified versus independent VRAM semantics;
live updates without replacing focused info controls; browser validation and
redeployment through the existing authorized graceful restart. Other agents'
changes, shared widgets, themes, serving configuration and resource mutations
remain outside this fix. Related: completed 0980/0981 and proposed 0982.
ADR impact: none; existing measurement ownership and authorization are unchanged.

## Acceptance criteria

- [x] No repeated reference totals, host overview, GPU utilization or explanatory paragraphs appear in the memory dialog.
- [x] Available RAM and GPU allocations/VRAM have a clear visual hierarchy; shared RAM versus separate VRAM and process/device scope remain visible.
- [x] Model weights, model KV and process memory appear once, with estimates, partial/unknown data and non-additivity explained by kit info icons.
- [x] Dialog renders compactly on desktop and mobile, uses shared themes, supports keyboard tooltips/Escape and preserves focus during live refresh.
- [x] Browser checks cover unified, dedicated, missing-weight, zero and unavailable states; the actual live page is redeployed and verified.

## Testing

`python -m pytest tests/test_console_resources_dashboard.py tests/test_gateway_console_responsive.py tests/test_gateway_console_gateway_card.py`

`ABSTRACTGATEWAY_BROWSER_TESTS=1 python -m pytest tests/test_gateway_console_browser_resources.py`

## Receipts

- Intake: operator screenshot and current served/source audit, 2026-10-30.


## Completion report

Completed 2026-10-30. Implementation: `08f963ff` (unreleased; deployed locally).
The previous inspector exposed the entire legacy panel, despite the compact
layout intended by 0980. It now renders two cards and three allocation figures;
all repeated references, GPU utilization, host facts and paragraphs are removed
from the visible dialog. Measurement explanations use the shared kit info icons.
Unified GPU allocations say “Shares RAM”; dedicated GPUs show available device
VRAM. Missing weights remain unknown; partial sums and estimated sizes are
marked. Info controls keep their DOM identity through the five-second refresh.

Changed `src/abstractgateway/console_resources.py`, browser checks in
`tests/browser/resources_dashboard.mjs`, and calculation tests in
`tests/test_console_resources_dashboard.py`. Existing hidden legacy IDs remain
for compatible render callers, outside the visible modal. Shared widgets/themes,
backend APIs, probes and resource operations are unchanged.

Validation: 18 focused tests passed; the browser test passed with 122 checks and
zero failures across dark/light themes and five viewport widths. Reviewed desktop,
mobile and dedicated-VRAM screenshots. Covered missing sizes, measured zero,
partial estimates, snapshot failure, hover/keyboard info, Escape and refresh focus.
Browser verification uses route-controlled host fixtures, not GPU hardware for
all topologies. The actual deployment verifies unified memory on this host.

Deployment receipt: authenticated graceful restart on the existing :8080 gateway
returned `{ok:true,restart:true}` after runner/model-host inflight counters were
zero. The gateway returned healthy. A live browser verified the deployed Memory
dialog: two cards, 44 visible words, hidden legacy panel, working hover and keyboard
tooltips, Escape layering, retained focus through refresh and zero page errors.
The kit intentionally suppresses a focus tooltip after pointer input; the initial
programmatic-focus check was corrected to exercise keyboard navigation and hover.
No tooltip implementation change was needed. No package release or new service.

Evidence: `design/resources/memory-inspector-validation.json` and
`design/resources/memory-inspector-live.png`. Intake commit: `2c6aea67`.
Original path: `planned/0983_simplify_memory_inspector.md`.
Final path: `completed/resources/0983_simplify_memory_inspector.md`.
Agora receipts/mirror unavailable because check_inbox returns connection refused;
this local audit records the operator-directed delivery.

Residuals/triage: no new implementation follow-up; retained history and serving
metrics remain proposed under 0982. Unknown source measurements are not made up.
ADR impact: none (presentation-only correction within existing measurement and
control contracts). No recurrent task was triggered. Counts and links updated;
legacy filename hygiene remains outside this focused fix.
