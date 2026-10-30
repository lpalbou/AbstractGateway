# 0994-abstractgateway: [IMPROVEMENT] Model signals, fit default and official brand marks

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: planned
> Priority: P1
> Labels: ux, seat-framework
> Work-item id: abstractgateway-0994
> Owner: framework
> Thread: unavailable (Agora connection refused)
> Hub refs: none

## Current code reality and scope

Operator specifies exact right-side order: capability icons, total parameter
count, fold chevron. 0993 currently places parameters before capabilities.
`mcRowCaps` already reads server vision=true; distinguish input vision from image
output, with a visible eye and themed colour. Use subtle token-derived coloured
capability discs. Show total parameters concisely; active-parameter detail belongs
in the shared tooltip. Default Fits this computer to on; explicit off must survive
URL/reload, and Downloaded must still expose all local inventory. No inferred
capabilities or model-name heuristics.

Add official manufacturer marks left of titles through shared UIC brandMarks
(0076), based on catalog vendor, embedded without new per-card network calls.
Unknown brands keep a neutral text fallback. Preserve the balanced 0993 spacing,
all versions/operations, themes and reserved chrome. No new ADR impact.

## Validation

Shared brand provenance and rendering checks, focused catalog/filter tests,
real-browser light/dark/mobile checks for exact order, vision semantics and
colour, fit default/off persistence and manufacturer logos, then scoped commits
and authorized local redeploy. Record explicit supported brands and fallback.

- [ ] Implement shared marks and model signals/defaults.
- [ ] Validate, commit, redeploy and save evidence.
