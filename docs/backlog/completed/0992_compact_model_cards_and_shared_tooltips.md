# 0992-abstractgateway: [IMPROVEMENT] Compact model cards and remove repetitive tooltips

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: completed
> Completed: 2026-10-30
> Release: unreleased (local deployment verified)
> Priority: P1
> Labels: ux, theme, seat-framework
> Work-item id: abstractgateway-0992
> Owner: framework
> Thread: unavailable (Agora connection refused; direct operator request)
> Hub refs: none

## Scope and current code reality

Operator screenshot shows oversized cards: separate capability/publisher lines,
large header/body gaps and 58px version rows. `console_catalog.py` owns cards.
Use three compact version rows, a tighter responsive grid, and accessible small
capability icons beside the title. Retain contrasting clickable headers, full
version disclosure, download/default/delete operations, loading, themes and
reserved chrome. Remove the redundant info-icon tooltip; keep exact build
identity/copy and cache/license details through a clearly interactive build
label, without duplicating visible size/fit prose. No new description/Hub requests.

The shared kit tooltip intentionally inverts theme colours; fix that in UIC
0075, then regenerate the console’s kit assets. Do not fork tooltip CSS locally.
Validate density and actual tooltip colours in light/dark and mobile, operations
and keyboard behavior; scoped commits and authorized local redeploy. No ADR
impact: presentation within existing contracts. Agora connection refused.

## Acceptance criteria

- [x] Requested visual/interaction changes implemented through existing contracts.
- [x] Focused checks and visible browser evidence retained.
- [x] Scoped implementation committed and local console redeployed/verified.

## Delivery and validation

Implemented in `a352234b`; shared AbstractUIC correction `fe951fd` (UIC 0075).
Cards now measure 151px high with three 36px rows and three columns at 1600px.
Capabilities are icons beside the title; margins are reduced; repetitive info
controls/tooltips removed. Exact build identity/copy remains on click. All
remaining kit tooltips follow active theme surface/text/border.

88 regression tests passed, then all 8 sync checks passed after correcting the
source fingerprint. 138 catalog/wizard + 122 Resources browser assertions and
shared kit tooltip/token/contrast checks passed. Healthy graceful redeploy and
live dark/light/mobile verification completed without management writes or page
errors. [Review and screenshots](../../../design/compact-models-0992/REVIEW.md),
[receipt](../../../design/compact-models-0992/validation.json).
No remaining work or ADR impact in this item; no new description requests.
