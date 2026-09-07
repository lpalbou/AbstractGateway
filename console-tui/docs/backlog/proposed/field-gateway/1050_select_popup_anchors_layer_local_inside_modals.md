# 1050 — Select/Combobox popups anchor in LAYER-LOCAL coordinates: displaced to the top-left inside Modals

- Status: proposed (operator screenshot 2026-07-25 06:54; headless-reproduced;
  root-caused in engine source same hour)
- Engine: abstracttui 0.2.12
- Severity: P1 (every dropdown inside every modal opens far from its field)

## Symptom

A Select inside a centered Modal opens its options popup at the screen's
top-left, far from the field (operator screenshot: sandbox modal centered
~col 45/row 17; popup at ~col 10/row 4 — exactly the field's position in
MODAL-LOCAL coordinates). Reproduced headless via the route editor's
provider Select at 110x34: popup painted offset from the field, over the
underlying screen.

## Root cause (engine source, 0.2.12)

- Overlay layers lay out their trees LAYER-LOCAL; the compositor applies
  the layer origin at paint time (`Layer::new(surface, bounds.origin(), z)`,
  overlays.rs:144; `rect_of(id)` → local).
- `Select` captures its anchor from the draw-time rect
  (select.rs:423 `.draw(move |_canvas, rect| last_rect.set(Some(rect)))`;
  key-open path uses `ctx.current_rect()` — same space).
- The popup opens via `place_panel(viewport, anchor, …)` (anchored.rs:115),
  which interprets the anchor in VIEWPORT coordinates and positions the
  popup layer in screen space.

Local rect + screen placement = displacement by the owning layer's
origin. Root-layer widgets are unaffected (origin 0,0) — which is why the
class survived every root-screen review and only shows inside Modals.
By symmetry every draw-rect-anchored surface is suspect: Combobox,
MultiSelect, anchored panels/completions, tooltips.

## Proposed engine fix (clean shape)

Translate anchors to screen space at the CAPTURE boundary — e.g. draw
context exposes the owning layer's origin (`ctx.layer_origin()`), and the
select-family anchor writes `rect + origin`; or `place_panel` callers
resolve the opener's layer origin. One translation, one boundary; no app
can work around this (layer origins are engine-private).

## Console-side status

No app-side workaround attempted (would be a hack over engine-private
geometry). Verification pin (popup renders adjacent to its field inside a
modal) will land console-side the day the engine fix ships.
