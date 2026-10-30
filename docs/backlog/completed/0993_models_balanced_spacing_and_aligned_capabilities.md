# 0993-abstractgateway: [IMPROVEMENT] Balanced Models spacing and aligned capability icons

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: completed
> Completed: 2026-10-30
> Release: unreleased (local deployment verified)
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

- [x] Implement and validate spacing/alignment.
- [x] Commit, redeploy and verify live.

## Completion report

Implemented `af7a33a4`; live cards measure 195px (45px header/44px rows), two
columns at 1600px. Capability icons align at the right before the chevron.
42 focused tests + 7 follow-up browser/merged checks pass; 140 browser assertions.
Healthy graceful redeploy, live dark/light/mobile and controls verified without
management writes or page errors. No ADR impact or new residual work.
[Evidence](../../../design/balanced-models-0993/REVIEW.md).
