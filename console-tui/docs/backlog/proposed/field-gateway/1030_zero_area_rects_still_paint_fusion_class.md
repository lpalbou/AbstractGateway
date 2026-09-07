# 1030 — Zero-area rects still PAINT: crushed rows fuse into their siblings (finding-0240 #4)

- Status: proposed (field finding from abstractgateway-console 0.3.3; adversarially verified)
- Engine: abstracttui 0.2.12
- Severity: P1-adjacent (visual corruption, not just absence)

## What happens

The worst artifact of the zero-collapse class is not disappearance — it
is FUSION. `UiTree::draw_node` (draw.rs:80) culls subtrees outside the
clip but deliberately exempts EMPTY rects, so a child flex-shrunk to
zero height still runs its draw closure with the degenerate rect. Draw
closures that only clip horizontally (the common hand-rolled text-row
shape — this console's `line_styled`, and by symmetry any app's) then
paint one full row of text onto whichever sibling legitimately owns
that y.

Observed live (adversarial review, 100x16): the review screen's crushed
intro line and the pinned button painted on the SAME row —
`│ Run a test (g) generation through the gateway to prove…│` was the
button overprinting the intro's first cells. The pre-fix vanishing
title bar was the same mechanism: the header was not gone, it was
OVERPAINTED by the tab bar.

No app-side recipe prevents this class — pinning every row means giving
up overflow absorption entirely. The engine's own `Separator` defends
itself with `if rect.h <= 0 return`, which is evidence the engine
expects these calls to happen.

## Proposal

In `draw_node`, skip `Paint::Draw`/`Paint::Text` when
`rect.is_empty()`, preserving the existing `probe_when_culled`
exemption for measurement nodes. This changes NO layout — it only
suppresses paint from boxes the solver assigned zero area, and nothing
legitimate paints from a 0x0 box (Separator already self-guards, so its
behavior is unchanged). Every zero-collapse then degrades from
CORRUPTION to CLEAN ABSENCE, which the 0240 #3 debug notice names.

## Companion app-side lesson (shipped console-side)

The 0240 #3 diagnostic publishes into `use_startup_notices` — this app
never rendered that signal, so the header crush was being NAMED in
every debug run into a lane nobody read. The console footer now renders
the latest engine notice whenever its own notice lane is idle. Any
abstracttui app should render that signal somewhere.
