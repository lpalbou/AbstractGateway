# 0993-abstractgateway: [IMPROVEMENT] Balanced Models spacing and aligned capability icons

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: planned
> Priority: P1
> Labels: ux, seat-framework
> Work-item id: abstractgateway-0993
> Owner: framework
> Thread: unavailable (Agora connection refused)
> Hub refs: none

## Current code reality and scope

0992 made 151px cards with three 36px rows and three columns at 1600px. Operator
finds this too dense. `console_catalog.py` owns the grid/rows/header. Increase
spacing to a balanced 44px row, roomier header and two columns at common desktop
widths. Put capability icons in a right-aligned header group before the chevron,
so their position does not follow title length. Retain three-version preview,
shared themes/tooltips, header affordance and all model controls. No data/API
or shared chrome changes. No ADR impact: presentation within existing contracts.

## Validation

Rendered browser light/dark and mobile: moderate card/row dimensions, capability
alignment across different title/parameter lengths, expansion/focus and operations.
Focused regression checks; scoped commit and authorized local redeploy. Preserve
prior 0992 history and append screenshots/evidence at completion.

- [ ] Implement and validate spacing/alignment.
- [ ] Commit, redeploy and verify live.
