# 0984-gateway: [IMPROVEMENT] Make the model catalog default to 4-bit, show loading and collapse versions

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: planned
> Priority: P1
> Labels: console, ux, seat-framework
> Work-item id: abstractgateway-0984
> Owner: framework
> Thread: unavailable (Agora connection refused; direct operator request)
> Hub refs: none

## Summary and code audit

Current code reality: `console_catalog.py` defaults quant to all, always renders
all matching versions and only shows loading text. The full extended catalog
must remain reachable (ADR-0026); filters already use provider quant_class.

Scope: default 4-bit with explicit All URL round-trip, five matching versions
per model with reversible disclosure, a visible accessible spinner during
catalog/Hub requests, accurate counts, focus retention, regression/browser checks
and the existing authorized local redeploy. No backend/catalog truncation.
ADR impact: none; disclosure keeps every match reachable.

## Acceptance criteria

- [ ] Bare Models opens with 4-bit selected; explicit All and existing deep links round-trip.
- [ ] Every matching model remains listed, with five versions until expanded and a Show fewer control.
- [ ] Initial and refresh/Hub loading show a spinner and accessible status; error recovery remains clear.
- [ ] Unit and browser checks exercise disclosure, filters, delayed loading, themes and keyboard behavior.

## Receipts

- Intake: operator screenshots and live source audit, 2026-10-30.
