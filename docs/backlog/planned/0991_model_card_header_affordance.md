# 0991-gateway: [IMPROVEMENT] Make expandable model-card headers recognizable and clickable

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: planned
> Priority: P1
> Labels: console, models, ux, seat-framework
> Work-item id: abstractgateway-0991
> Owner: framework
> Thread: unavailable (Agora connection refused; direct operator request)
> Hub refs: none

## Summary and current code reality

Operator screenshot: model header shares the body background and only the
chevron indicates expansion. `console_catalog.py` owns cards; native named
chevron buttons already preserve keyboard focus and full version reachability.
Add a distinct header band using shared theme tokens, subtle hover/focus
highlight and pointer affordance. Clicking anywhere in an expandable header
must toggle exactly once through its existing native button. Headers with no
extra versions must not imply interactivity. Preserve three-row previews,
loading, shared chrome/themes, management controls and mobile layout. No ADR
impact: local presentation inside existing contracts. No shared-kit fork.

## Acceptance criteria

- [ ] Header background is visibly distinct in dark/light themes.
- [ ] Expandable header highlights on hover/focus and clicks across its full area.
- [ ] Native chevron keyboard semantics/focus remain correct; no double toggle.
- [ ] Browser visual/behavior verification, scoped commit and authorized redeploy.
