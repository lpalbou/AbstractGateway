# Model header affordance — 0991

Single-agent implementation/visual review. [Live header](header-live.png),
[hover](header-hover-live.png), [320px light fixture](mobile-light-fixture.png).
Headers use the shared surface/border/radius tokens with a subtle themed
hover/focus blend. The full expandable header activates its existing named
native button; no nested control, duplicate tab stop or double toggle was added.
Non-expandable headers retain the band but have no pointer/hover affordance.

42 targeted tests and 124 catalog/wizard browser assertions passed. Light/dark,
mobile, pointer/title/metadata activation, keyboard focus and disclosure,
non-expandable headers, loading and management regressions were covered.
Live healthy console verified distinct background, hover/focus highlights,
full-header pointer/click behavior and Space/Enter expansion/collapse; no page
errors or management writes. The first live check raced progressive enrichment;
the final check waited for its completion before asserting focus styling.

Committed `549680a7` and redeployed through the existing graceful restart with
zero runner/model operations in flight. Shared kit/themes and reserved widgets
preserved. No additional follow-up or ADR impact. Machine-readable receipts:
[validation.json](validation.json).
