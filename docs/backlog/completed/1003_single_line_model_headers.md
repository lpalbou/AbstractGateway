# 1003-abstractgateway: [IMPROVEMENT] Single-line model headers

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: completed
> Completed: 2026-10-30
> Release: unreleased (local deployment verified)
> Priority: P1
> Labels: catalog, ux, seat-framework
> Work-item id: abstractgateway-1003
> Owner: framework
> Thread: local
> Hub refs: none

## Current code reality and scope

1002 put model names and capability/parameter/disclosure signals on separate
header rows. Operator explicitly requests a single-line header. Restore one
unwrapped row with the name, fixed capability slots, parameters and counted
chevron. Preserve readable names, themes and hover/focus behaviour. Move the
Recommended label beside the primary build so it cannot squeeze the header.
Use an independently scrollable header on narrow cards instead of wrapping,
clipping names or overflowing the page. Preserve 24px capability targets and
44px touch disclosure. Keep the existing local-only catalogue flow unchanged.

## Validation

Single-row geometry on desktop, narrow and touch cards; readable long recommended
names, one recommendation badge across disclosure/download redraws, capability
alignment, no page overflow, model actions and keyboard controls. Focused gates
and visible dark/light/mobile review. Scoped commit, authorized healthy local
restart and live validation. Core and unrelated changes remain untouched.

- [x] Implement and validate the one-line header.
- [x] Commit, redeploy and retain evidence.

## Completion report

Gateway `046f49fe` puts all header content on one line. Recommended moves beside
the primary build and remains once per card through expansion/download redraws.
Desktop names remain readable; narrow headers pan internally without page
horizontal overflow. Fixed capability slots, header hover/focus, disclosure and
local-only catalogue requests are preserved.

51 focused tests, 264 Chromium assertions and live dark/light/390px/320px/touch
checks pass. Header elements share one row at every verified width; the first six
desktop header heights are 45px. Full recommended name fits, model operations and
keyboard disclosure work, no page errors or model-management writes. Healthy
authorized local restart verified. [Review and captures](../../../design/model-headers-1003/REVIEW.md).
No Core changes, new ADR boundary, publication, persistence or required follow-up.
