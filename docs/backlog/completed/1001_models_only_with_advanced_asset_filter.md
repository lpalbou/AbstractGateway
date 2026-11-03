# 1001-abstractgateway: [IMPROVEMENT] Models only with advanced asset filters

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: completed
> Completed: 2026-10-30
> Release: unreleased (local deployment verified)
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

- [x] Implement and validate the correction.
- [x] Commit, redeploy and retain review evidence.

## Completion report

Gateway `72079bc2` / `5abd3123` default browsing and Downloaded to full models,
with an advanced Asset type facet for adapters/components/unclassified/all.
Counts follow the selected type, URLs preserve it, hidden assets do not leak
through legacy fallback rows, and normal model filters restore on return.
Reloaded adapter links preserve the all-precisions Downloaded model scope.
Shared structured tooltips show adapter type, declared base, task and triggers
when supplied; no new standalone-model badges/defaults are inferred.

38 Gateway unit checks and 248 Chromium assertions passed; 168 Core tests passed
in both source and deployment checkout. Core `0062fa69` deployed as `449aefce`.
Healthy authorized graceful redeploy verified live: default/Downloaded models
only, 44 adapters reachable explicitly, 12 components and 26 unclassified entries
behind advanced facets. Dark/light/reload/390px/320px/touch checks pass without
page errors, overflow, fallback rows or model-management writes.
[Review and captures](../../../design/model-types-1001/REVIEW.md),
[validation](../../../design/model-types-1001/validation.json).

This supersedes the ordinary-list scope of [1000](1000_render_discovered_catalog_assets.md)
without discarding its full inventory accounting. AbstractVision already has the
specific adapter inference registry; Core adds separate schema-backed metadata
without duplicating those rules. No new ADR impact, persistence or publication;
no residual required work. Agora unavailable; local evidence carries receipts.
