# 1001-abstractgateway: [IMPROVEMENT] Models only with advanced asset filters

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: planned
> Priority: P1
> Labels: catalog, ux, seat-framework
> Work-item id: abstractgateway-1001
> Owner: framework
> Thread: unavailable (Agora connection refused)
> Hub refs: none

## Current code reality and scope

0875/1000 made all cache assets first-class catalogue rows, but ordinary browsing
and Downloaded currently include adapters as peer model cards. Operator feedback
requires full models by default; advanced users explicitly select other asset types.

Default the catalogue and Downloaded view to full model entries. Put adapters, components and unclassified cache files behind the advanced Filters asset-type facet, with explicit all-assets inspection. Preserve search/deep links, coherent scoped counts, safe deletion and model defaults. Render Core adapter compatibility metadata in the existing shared structured tooltip. Supporting assets must not surface through fallback rows or capability searches by default.

## Validation

Offline local-cache fixtures and schema validation; default/Downloaded/full-model
filter tests, advanced facets/counts and URL restoration, mixed inventories,
keyboard and light/dark/mobile browser checks. Scoped commits and authorized
local redeploy with actual Core worktree, healthy graceful restart and live
verification. No model writes or new persistent service installation.

- [ ] Implement and validate the correction.
- [ ] Commit, redeploy and retain review evidence.
