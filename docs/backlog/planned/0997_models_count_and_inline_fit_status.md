# 0997-abstractgateway: [FIX] Simple model count and inline fit status

> Package: abstractgateway
> Type: fix
> Created: 2026-10-30
> Status: planned
> Priority: P1
> Labels: ux, seat-framework
> Work-item id: abstractgateway-0997
> Owner: framework
> Thread: unavailable (Agora connection refused)
> Hub refs: none

## Current code reality and scope

`mcCountMarkup` adds a matching-version total to the model count. Remove that
extra summary; keep the filtered/total model count and provisional loading truth.
All versions remain available behind each card's chevron.

The build grid reserves a fixed 42px status column while its flex container
wraps. An installed folder plus Tight or Fits can therefore split onto two lines.
Size this column by its content and keep status icons/text together, including
longer fit labels, loaded-state controls and narrow cards. Preserve operations,
card spacing, tooltips and the responsive layout. No new ADR impact.

## Validation

Update existing count expectations while retaining underlying inventory/filter
and disclosure coverage. Check actual folder/status vertical alignment and
non-overlap at desktop and phone widths in light/dark, then scoped commit,
authorized graceful local redeploy and live verification. No unrelated changes.

- [ ] Simplify the counter and keep the status cluster on one line.
- [ ] Verify, commit, redeploy and retain completion evidence.
