# 0999-abstractgateway: [IMPROVEMENT] Models toolbar and capability matrix

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: planned
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

- [ ] Implement the requested toolbar, widget and header layout.
- [ ] Validate, commit, redeploy and retain evidence.
