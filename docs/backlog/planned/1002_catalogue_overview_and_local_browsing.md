# 1002-abstractgateway: [IMPROVEMENT] Catalogue overview and local browsing

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: planned
> Priority: P1
> Labels: catalog, ux, performance, seat-framework
> Work-item id: abstractgateway-1002
> Owner: framework
> Thread: unavailable (Agora connection refused)
> Hub refs: none

## Current code reality and scope

The catalogue always launches a local preview plus a Hub/extended request, shows
Loading more versions above usable cards, and automatically searches the Hub on
local text input. The operator rejects this flow on the catalogue. Variable
three-build previews leave grid gaps, while the nine fixed capability slots and
Recommended label squeeze titles to ellipses. Existing tests explicitly pin that
old behaviour; update them to validate the corrected user flow rather than
keeping a spinner or layout defect to satisfy a check.

Use one local catalogue request, concurrent optional inventory discovery, and
keep local search local. Explicit Hugging Face mode retains online search and
all upstream quantizations. Catalogue All means all catalogue-known builds;
that intentionally supersedes the earlier eager-Hub implementation in light of
this operator correction. No synthetic version counts or hidden matching data.

Render consistent two-column overview cards with a readable full title, an
aligned capability/parameter/disclosure strip, one primary build, and a counted
header chevron to expose every matching build. Prefer the active default/ready
installed build, otherwise the recommended build; keep ordering stable while
inspecting an expanded card. Preserve useful tooltips, shared chrome/theme,
advanced asset types, keyboard use, confirmation and exact action identifiers.

## Validation

Meaningful delayed/offline request tests, no background Hub access on catalogue
entry/search/downloaded/refresh, explicit Hub search/page and all-quant behaviour,
card ordering/disclosure, full titles, mixed one/many-build card geometry, all
existing model operations and filters. Direct visible UX review in both themes,
320px/touch and keyboard paths. Scoped commits, authorized local restart and
live request/latency verification; no live model mutations or Core checkout
changes. No new ADR boundary; existing Core/Runtime catalogue ownership applies.

- [ ] Implement and verify the corrected flow and layout.
- [ ] Commit, redeploy and retain completion/UX evidence.
