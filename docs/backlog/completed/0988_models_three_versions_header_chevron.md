# 0988-gateway: [IMPROVEMENT] Show three model versions with a header chevron

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: completed
> Completed: 2026-10-30
> Priority: P1
> Labels: console, models, ux, seat-framework
> Work-item id: abstractgateway-0988
> Owner: framework
> Thread: unavailable (Agora connection refused; direct operator request)
> Hub refs: none

## Summary and current code reality

Operator refinement of completed 0987: cards currently show five versions and
have a footer Show more button in `console_catalog.py`. Show three versions and
move disclosure to a chevron at the right of the card header. Keep every version
reachable, matching counts truthful, keyboard focus and aria-expanded/controls
intact. Preserve shared themes/chrome and management operations. No ADR impact.

## Acceptance criteria

- [x] Three versions initially; full expansion and collapse via header-right chevron.
- [x] Accessible name, tooltip, expanded state and keyboard focus work in both themes/mobile.
- [x] Relevant tests pass; scoped commit and authorized local redeploy verified.

## Completion report

Completed 2026-10-30 in Gateway `ce0c2e12`, followed by loading refinements in
0986. Original path: `planned/0988_models_three_versions_header_chevron.md`;
final path: `completed/0988_models_three_versions_header_chevron.md`.

Cards show three matching versions by default. The footer disclosure was removed;
a chevron at the header's right expands/collapses the full matching set. It has
an accessible model-specific name, shared-kit tooltip, aria-expanded/controls,
visible focus and a rotated expanded state. Counts include every matching
version. Every version, exact ID and management operation remains reachable.
Shared themes and reserved header widgets remain intact.

Validation shared with [0986](0986_catalog_fetch_latency.md): 81 console regression
tests passed, including 112 catalog/wizard and 122 Resources browser assertions.
Keyboard expansion/collapse and focus, desktop/mobile and light/dark covered.
Live healthy console verified three versions, header chevron, expansion to all
matching versions, collapse to three, details and shared chrome; no browser
errors or management writes. [Screenshots and receipts](../../../design/catalog-loading-0986-0988/REVIEW.md).
Scoped changes committed and locally redeployed through the existing restart.
No additional follow-up for disclosure; loading residuals are recorded by 0986.
ADR impact: none. Agora unavailable, local audit evidence retained.
