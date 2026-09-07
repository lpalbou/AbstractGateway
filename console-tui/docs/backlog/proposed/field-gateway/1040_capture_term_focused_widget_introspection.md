# 1040 — CaptureTerm: expose the focused widget for headless tests (focus-walk fragility)

- Status: proposed (field finding from abstractgateway-console 0.3.4, round-4 review)
- Engine: abstracttui 0.2.12
- Severity: P3 (test ergonomics)

## The fragility

Headless tests drive forms by tab-walking to a target widget with bare
loop counts (`for _ in 0..10 { key(TAB) }`). Adding one field to a form
breaks N tests with "validation error not found" instead of "focus walk
changed" — the failure names the SYMPTOM, not the drift. App-side
mitigation exists (walk-then-assert-marker helpers), but the honest fix
is engine-side.

## Ask

Expose focused-widget identity on the capture harness — e.g.
`CaptureTerm::focused_id() -> Option<&str>` (element id / debug label /
widget kind), or a `Driver::focus_path()` render of the focus chain.
Tests could then `tab_until(|id| id == "save-button")` and fail with
"never reached save-button (walked: url, token, probe…)" — drift
becomes self-naming. Zero production cost if gated to the testing
module.
