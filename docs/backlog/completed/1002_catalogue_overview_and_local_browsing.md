# 1002-abstractgateway: [IMPROVEMENT] Catalogue overview and local browsing

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: completed
> Completed: 2026-10-30
> Release: unreleased (local deployment verified)
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

- [x] Implement and verify the corrected flow and layout.
- [x] Commit, redeploy and retain completion/UX evidence.

## Completion report

Gateway `5c766ce3` replaces eager local-preview/Hub-enrichment loading with one
local catalogue request and concurrent optional local inventory discovery.
Typing/Downloaded/filtering/disclosure stay local. Online search, paging and
upstream quantizations remain available by explicitly selecting Hugging Face;
source changes restore the local query/filters. This intentionally supersedes
the earlier eager-quant policy in response to the operator's UX correction.

Cards now show one primary build, full readable model names, a separate aligned
capability/parameter strip and counted header disclosure for all matching builds.
Ready/default builds take priority, expanded comparisons retain ordering, and
deleting a visible build leaves feedback when it disappears. Shared chrome,
themes, advanced assets and confirmation paths are preserved.

51 focused tests and 262 Chromium assertions passed. Healthy authorized graceful
local redeploy verified in dark/light/390px/320px/touch and keyboard flows.
Latest live first-card times were 1.561s and 2.040s, one initial catalogue read;
only explicit refresh adds another local read. No Hub query, page errors or live
model-management writes. All 88 default and 71 downloaded overview cards have
one build; the first six heights are uniformly 141px in both views. Full model
names, fixed capability columns, no-match recovery and explicit expansion pass.
[Review, captures and limits](../../../design/catalogue-overview-1002/REVIEW.md),
[direct UX review](../../../design/catalogue-overview-1002/UX_REVIEW.md),
[live validation](../../../design/catalogue-overview-1002/validation.json).

No Core/deployment-worktree changes, new ADR boundary, publication, persistence
or residual required work. Expanded comparisons intentionally grow; subjective
aesthetic acceptance remains the operator's. Prior work/history retained.
Agora unavailable (connection refused); local records carry the receipts.
