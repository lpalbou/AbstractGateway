# 0991-gateway: [IMPROVEMENT] Make expandable model-card headers recognizable and clickable

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: completed
> Completed: 2026-10-30
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

- [x] Header background is visibly distinct in dark/light themes.
- [x] Expandable header highlights on hover/focus and clicks across its full area.
- [x] Native chevron keyboard semantics/focus remain correct; no double toggle.
- [x] Browser visual/behavior verification, scoped commit and authorized redeploy.

## Completion report

Completed 2026-10-30. Intake `37519f3f`; implementation `549680a7`.
Original path: `planned/0991_model_card_header_affordance.md`.
Final path: `completed/0991_model_card_header_affordance.md`.

Added a full-width themed header band and border; expandable headers highlight
on hover/focus and show a pointer/accent chevron. Full-header pointer activation
uses the existing native named button, preserving keyboard semantics and focus
without a second tab stop or double toggle. Non-expandable headers do not imply
interactivity. Mobile spacing and reduced-motion preferences are respected.
Only `console_catalog.py` and its browser checks changed; no shared-kit changes.

42 targeted tests passed; 124 browser assertions passed in light/dark desktop
and mobile. Live healthy console verified contrast, hover/focus, title and header
whitespace clicks, Space/Enter expansion/collapse, no errors and no management
writes. [Screenshots/review](../../../design/model-header-0991/REVIEW.md) and
[receipts](../../../design/model-header-0991/validation.json) retained. Scoped
implementation committed and redeployed using the existing graceful restart,
with zero runner/model work in flight. No new services/persistence or release.
No other agents' work staged. No follow-up, ADR impact or recurrent task trigger.
Agora unavailable (connection refused); local evidence retained without claiming
hub receipt/mirror synchronization. Existing unrelated 0989/0990 proposals remain.
