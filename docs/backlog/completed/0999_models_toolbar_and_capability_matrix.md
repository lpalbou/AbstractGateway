# 0999-abstractgateway: [IMPROVEMENT] Models toolbar and capability matrix

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: completed
> Completed: 2026-10-30
> Release: unreleased (local deployment verified)
> Priority: P1
> Labels: ux, accessibility, seat-framework
> Work-item id: abstractgateway-0999
> Owner: framework
> Thread: unavailable (Agora connection refused)
> Hub refs: none

## Current code reality and scope

Models still shows gpu_name and refresh in a separate host row; the top-bar
resource widget already owns the live host snapshot but omits its GPU name.
Move the machine label into that widget and refresh immediately after Filters.
Shorten the search field by approximately 10% at desktop widths and rename the
single quick toggle to Downloaded. Keep shared reserved widgets and themes.

`mcCapabilityMarkup` currently packs supported icons, so their positions vary.
Reserve one fixed slot for every capability, with blank non-focusable slots
where the metadata does not report it. Preserve useful shared explanations,
colours and keyboard access. Keep names readable and slots aligned on narrow
cards without introducing unsupported capability claims.

## Validation

Unit checks for live machine-name unknown/stale states; existing catalog and
Resources gates; real-browser toolbar order, renamed toggle, capability slot
geometry across mixed models, contrast, keyboard and narrow layout. Scoped
commits, authorized local redeploy and live verification with saved evidence.
Existing UI ownership remains; no new ADR, registry publication or model actions.

- [x] Implement the requested toolbar, widget and header layout.
- [x] Validate, commit, redeploy and retain evidence.

## Completion report

`cde6956e` / `4a69ff88` move the live machine name into the memory/GPU widget,
remove the Models host row, group refresh after Filters, shorten the desktop
search field and label the switch Downloaded. Fixed capability and disclosure
slots align cards with different reported capabilities and version counts.
Empty cells are inert; theme, meaningful tooltips and keyboard access retained.

70 primary checks, 7 widget checks, 35 disclosure follow-up checks and 15 final
responsive checks passed (overlapping gates); 222 catalog browser assertions.
Healthy graceful redeploy and live dark/light/390px/320px/touch checks passed.
All 88 browse / 29 downloaded cards align. Desktop search measures 10.02% less
than its unrestricted width. Full phone machine name and grouped actions verified;
no page errors, management writes or horizontal overflow.
[Review and captures](../../../design/model-matrix-0999/REVIEW.md),
[validation](../../../design/model-matrix-0999/validation.json).
No new ADR impact, publication or residual work; Agora unavailable. Existing
Core deployment and unrelated changes preserved.
