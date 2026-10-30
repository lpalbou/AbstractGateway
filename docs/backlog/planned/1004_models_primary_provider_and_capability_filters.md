# 1004-abstractgateway: [IMPROVEMENT] Primary provider and capability filters

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: planned
> Priority: P1
> Labels: catalog, ux
> Work-item id: abstractgateway-1004
> Owner: framework
> Thread: local

## Current code reality and scope

Provider and Capability are currently chips hidden in advanced Filters. The Status
facet duplicates the always-visible Downloaded switch and reports build counts
that differ from model totals. Operator requests a two-row main filter bar:
source/search/toggles above, Provider and Capability always visible below, with
summary/advanced/refresh controls alongside. Use existing themed form controls
and shared filter-bar shell; advanced contains only asset type and quantization.
Remove the redundant Status facet. Preserve linked filter values, clear/reset,
keyboard focus, local-only browsing, one-line card headers and advanced assets.

## Validation

Functional filter and URL tests; visible provider/capability controls while
advanced is closed; no redundant Status controls. Check exact two-row desktop
layout, responsive/narrow layout, keyboard selection/focus, interactions with
Downloaded/asset type/reset and local-only requests. Existing model-operation
and theme gates. Scoped commit, healthy authorized local redeploy and saved
live dark/light/mobile evidence; Core and unrelated changes untouched.

- [ ] Implement and verify the filter layout.
- [ ] Commit, redeploy and retain completion evidence.
