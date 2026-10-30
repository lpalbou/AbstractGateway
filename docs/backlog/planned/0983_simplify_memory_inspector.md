# 0983-gateway: [IMPROVEMENT] Replace the verbose memory inspector with a compact visual summary

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: planned
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

- [ ] No repeated reference totals, host overview, GPU utilization or explanatory paragraphs appear in the memory dialog.
- [ ] Available RAM and GPU allocations/VRAM have a clear visual hierarchy; shared RAM versus separate VRAM and process/device scope remain visible.
- [ ] Model weights, model KV and process memory appear once, with estimates, partial/unknown data and non-additivity explained by kit info icons.
- [ ] Dialog renders compactly on desktop and mobile, uses shared themes, supports keyboard tooltips/Escape and preserves focus during live refresh.
- [ ] Browser checks cover unified, dedicated, missing-weight, zero and unavailable states; the actual live page is redeployed and verified.

## Testing

`python -m pytest tests/test_console_resources_dashboard.py tests/test_gateway_console_responsive.py tests/test_gateway_console_gateway_card.py`

`ABSTRACTGATEWAY_BROWSER_TESTS=1 python -m pytest tests/test_gateway_console_browser_resources.py`

## Receipts

- Intake: operator screenshot and current served/source audit, 2026-10-30.
