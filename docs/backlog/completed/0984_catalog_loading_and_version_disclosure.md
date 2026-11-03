# 0984-gateway: [IMPROVEMENT] Make the model catalog default to 4-bit, show loading and collapse versions

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: completed
> Completed: 2026-10-30
> Priority: P1
> Labels: console, ux, seat-framework
> Work-item id: abstractgateway-0984
> Owner: framework
> Thread: unavailable (Agora connection refused; direct operator request)
> Hub refs: none

## Summary and code audit

Current code reality: `console_catalog.py` defaults quant to all, always renders
all matching versions and only shows loading text. The full extended catalog
must remain reachable (ADR-0026); filters already use provider quant_class.

Scope: default 4-bit with explicit All URL round-trip, five matching versions
per model with reversible disclosure, a visible accessible spinner during
catalog/Hub requests, accurate counts, focus retention, regression/browser checks
and the existing authorized local redeploy. No backend/catalog truncation.
ADR impact: none; disclosure keeps every match reachable.

## Acceptance criteria

- [x] Bare Models opens with 4-bit selected; explicit All and existing deep links round-trip.
- [x] Every matching model remains listed, with five versions until expanded and a Show fewer control.
- [x] Initial and refresh/Hub loading show a spinner and accessible status; error recovery remains clear.
- [x] Unit and browser checks exercise disclosure, filters, delayed loading, themes and keyboard behavior.

## Receipts

- Intake: operator screenshots and live source audit, 2026-10-30.


## Completion report

Completed 2026-10-30, unreleased; locally deployed. Gateway implementation
`334a2e6a`; intake `ae884b88`. Shared kit changes: `3b99aa4`, `8a25c33`.

Validation across the changed surfaces: 71 console/catalog/first-run/brand/sync
checks passed in the project environment; 15 credential-route, Resources logic
and Resources browser tests passed using the deployment environment (Runtime
0.15.3). The first credential-route attempt in the project environment failed
at the pre-existing Runtime 0.15.2 floor; it passed with the deployment Runtime.
No dependency environment was modified. Total: 86 passing targeted tests across
the two lanes. Browser scenarios contain 52 catalog/wizard checks and 122
Resources checks, all passing. Kit: TypeScript build and strict unused checks,
CSS balance, 21 theme-token invariants, 289 mark checks and 64 cloud-flow checks.
Reviewed desktop dark/light, mobile and delayed-loading screenshots. The small
first-run VM now binds real kit markup helpers; its synthetic DOM omits the
unrelated Resources initializer, covered by the real browser suites instead.

Deployment receipt: existing authenticated graceful restart returned
`{ok:true,restart:true}` after runner/model-host inflight were zero. Health
returned healthy. Live browser inspection exercised All (96 models, 1,798
matching versions), expansion/collapse, the five-row presentation, and the
wizard's six shared cards. The Providers tab was separately opened through its
navigation button: six cards, no page errors. No model operation or provider
credential write was performed during live QA. Source/artwork provenance is in
AbstractUIC `docs/icons.md`. Fixture screenshots and the validation receipt are
in `design/console-0984-0985/`.

Agora receipts/mirror were unavailable (connection refused); this local record
preserves the directly authorized delivery. ADR impact: none; existing shared
kit, measurement and endpoint-profile ownership remain in place. No package was
published and no service/persistence was installed.

The catalog defaults to 4-bit; All is represented explicitly as `quant=all`
so it survives shared links and reloads. Five matching versions per model are
rendered initially, with Show N more / Show fewer and keyboard focus retention.
Filtering is applied before disclosure, so matching versions remain reachable.
Counts say matching versions, not shown artifacts. Initial/refresh/Hub loading
uses an accessible spinner and honors reduced-motion preferences. No models or
server catalog data are capped.

Touched `console_catalog.py` and catalog/first-run regression/browser checks.
Residual: this reduces rendering work, while the unchanged full extended catalog
fetch can still take a long time. The first live load succeeded; later cold/fresh
reads exceeded a 12s HTTP probe and 30s browser waits. Spinner was visible and
health stayed healthy. Performance diagnosis/progressive retrieval is preserved
as [0986 (subsequently completed)](0986_catalog_fetch_latency.md), not claimed solved
by the display filter. No further backend changes are hidden in this delivery.

Original path: `planned/0984_catalog_loading_and_version_disclosure.md`.
Final path: `completed/0984_catalog_loading_and_version_disclosure.md`.
Counts, ledger and links updated; legacy filename hygiene remains outside this change.

Final fresh-page live receipt: 4-bit selected by default, 83 matching model cards,
349 version rows, every card at or below five versions, zero page errors. This
load completed when allowed a longer wait; the latency observation remains valid.
