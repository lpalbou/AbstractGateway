# 0998-abstractgateway: [IMPROVEMENT] Clear recommendation badge and one library control

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: completed
> Completed: 2026-10-30
> Release: unreleased (local deployment verified)
> Priority: P1
> Labels: ux, catalog, seat-framework
> Work-item id: abstractgateway-0998
> Owner: framework
> Thread: unavailable (Agora connection refused)
> Hub refs: none

## Current code reality and scope

Replace the low-contrast Starter label with a clearly visible, explained Recommended badge. Remove duplicate Browse/Downloaded controls; keep the nearby Downloaded only switch and existing filter/URL behavior. Preserve themes, host controls and header layout. Current code reality: mcLibraryMarkup emits two tabs in addition to the switch; mc-badge uses accent-on-accent-subtle. Validate browser, keyboard, responsive and local live behavior.

No registry publication or model downloads/inference required. Existing shared
ownership applies; record ADR impact at completion. Commit and authorized
local redeploy, retain validation and review evidence.

- [x] Implement and verify the requested behavior.
- [x] Commit, deploy locally and record completion evidence.

## Completion report

Implemented `334dd3bb`: one Downloaded only toggle, no duplicate tabs or repeated
Downloaded summary; visible outlined Recommended star badge and useful explanation.
Shared chrome, URL/reload/keyboard, filter restoration and card spacing preserved.
36 Gateway tests, 214 browser assertions and live dark/light/320px checks pass.
Recommended label contrast is 13.14:1 dark / 15.09:1 light. Coordinated Core 0873/0874
adoption verified: 102 catalogue models, all 23 LM Studio artifacts mapped and
reachable, zero local extras. Healthy graceful redeploy; no management writes.
[Review and captures](../../../design/model-library-catalog-0998/REVIEW.md),
[validation](../../../design/model-library-catalog-0998/validation.json).
No new ADR impact or residual UI work. Agora unavailable; unrelated work excluded.
