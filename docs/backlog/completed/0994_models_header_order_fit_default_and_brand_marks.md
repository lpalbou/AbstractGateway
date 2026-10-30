# 0994-abstractgateway: [IMPROVEMENT] Model signals, fit default and official brand marks

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: completed
> Completed: 2026-10-30
> Release: unreleased (local deployment verified)
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

- [x] Implement shared marks and model signals/defaults.
- [x] Validate, commit, redeploy and save evidence.

## Completion report

Implemented `9fd6038a` / `b3908d01`, adopting shared kit `4d0347e` (UIC 0076).
Exact capability/total-parameter/chevron order; explicit coloured input vision,
Fits on by default with durable off links, compact official brand marks left of
model names, and readable narrow-card titles. No inferred capabilities or extra
remote image requests. Downloaded/Browse filter behavior preserved.

Final gates: 33 catalog/browser/sync and 7 narrow-header/merged checks pass;
162 browser assertions; 492 shared brand checks plus tooltip/top-bar/theme guards.
Healthy graceful redeploy and live dark/light/mobile/controls verified with no
model-management writes or page errors. Note frontend persistence also verified.
[Review, supported brands and limits](../../../design/model-signals-0994/REVIEW.md),
[validation](../../../design/model-signals-0994/validation.json).
No new residual work or ADR impact. Unknown vendors retain text fallback.
