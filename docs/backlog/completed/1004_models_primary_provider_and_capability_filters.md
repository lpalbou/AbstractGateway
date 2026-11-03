# 1004-abstractgateway: [IMPROVEMENT] Primary provider and capability filters

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: completed
> Completed: 2026-10-30
> Release: unreleased (local deployment verified)
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

- [x] Implement and verify the filter layout.
- [x] Commit, redeploy and retain completion evidence.

## Completion report

Gateway `2dd60d51` / `a6529057` exposes Provider and Capability as themed selectors
on the second primary row. Model summary/Filters/refresh sit alongside them.
The duplicate advanced Status row is removed; advanced contains asset type and
quantization. Selected primary values are visible directly in their controls,
round-trip through the URL and retain keyboard focus. Downloaded resets them for
the library and restores them on exit. Legacy status links remain supported.

51 focused tests and 286 Chromium assertions pass. Live dark/light checks verify
two aligned rows at 1600px and 1024px, both selectors, reload and Downloaded
restoration. 390px/320px and touch checks pass without page overflow. One-line
headers and local-only catalogue reads are preserved, with no page errors,
model-management writes or Hub requests. Healthy idle restart and new served
markup verified. [Review and captures](../../../design/model-filters-1004/REVIEW.md).
No Core changes, new ADR boundary, publication, persistence or required follow-up.
