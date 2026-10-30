# 0998-abstractgateway: [IMPROVEMENT] Clear recommendation badge and one library control

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: planned
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

- [ ] Implement and verify the requested behavior.
- [ ] Commit, deploy locally and record completion evidence.
