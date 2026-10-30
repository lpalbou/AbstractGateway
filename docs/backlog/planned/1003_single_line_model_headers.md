# 1003-abstractgateway: [IMPROVEMENT] Single-line model headers

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: planned
> Priority: P1
> Labels: catalog, ux, seat-framework
> Work-item id: abstractgateway-1003
> Owner: framework
> Thread: unavailable (Agora connection refused)
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

- [ ] Implement and validate the one-line header.
- [ ] Commit, redeploy and retain evidence.
