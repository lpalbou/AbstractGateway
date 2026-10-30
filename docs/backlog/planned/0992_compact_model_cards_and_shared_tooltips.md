# 0992-abstractgateway: [IMPROVEMENT] Compact model cards and remove repetitive tooltips

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: planned
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

- [ ] Requested visual/interaction changes implemented through existing contracts.
- [ ] Focused checks and visible browser evidence retained.
- [ ] Scoped implementation committed and local console redeployed/verified.
