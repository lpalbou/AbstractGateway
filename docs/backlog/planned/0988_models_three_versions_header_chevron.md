# 0988-gateway: [IMPROVEMENT] Show three model versions with a header chevron

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: planned
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

- [ ] Three versions initially; full expansion and collapse via header-right chevron.
- [ ] Accessible name, tooltip, expanded state and keyboard focus work in both themes/mobile.
- [ ] Relevant tests pass; scoped commit and authorized local redeploy verified.
