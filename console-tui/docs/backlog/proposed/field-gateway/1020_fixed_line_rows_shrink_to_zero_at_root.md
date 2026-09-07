# 1020 — `LayoutStyle::line(1)` rows silently shrink to zero at the ROOT level (0240's class beyond modals)

- Status: proposed (field finding from abstractgateway-console 0.3.2)
- Engine: abstracttui 0.2.12
- Severity: P2 (silent chrome loss; app-side recipe exists)

## What happened (operator-reported, headless-reproduced)

The console's root layout is `column → header(line(1)) → PageHost(grow 1.0) → footer`.
The 1-line title bar rendered fine on every screen — until a page whose
loaded content minimum over-demands height (Users & Entities with two
populated tables) made the root column flex-shrink the fixed header row
to ZERO. The PageHost tab bar then painted at row 0. No warning, no
clipping — the title bar just wasn't there (operator screenshot
2026-07-24; reproduced headless with fixtures at 110x34).

Finding 0240 documented this class for MODAL content ("overflowing modal
content flex-shrinks fixed rows (buttons!) to zero silently"). This
report extends it: the same silent-shrink bites ROOT chrome, where the
loss is a whole app-identity surface, and the trigger is data volume —
an app that looked correct through every empty-fixture review loses its
header the first time a real gateway serves seven entities.

## App-side fix (shipped in console 0.3.2)

`.shrink(0.0)` on the header row, the separator row, and the footer's
chrome rows — the 0240 recipe applied at root. Pinned by a headless test
(`title_bar_and_separator_survive_content_pressure`: 6 screens × 2 modes
× heavy fixtures × 100x24).

## Engine asks — SETTLED after adversarial round 3 (2026-07-24)

The original ask 1 ("`line(n)` implies `shrink(0.0)`") is WITHDRAWN
after arguing both sides: the CSS-like yield default is documented as
deliberate (style.rs), this console itself uses `line(1)` for blank
filler rows where yielding is desirable, and flipping a published
default silently re-layouts every existing app — the exact compat
hazard the engine's 0240 #3 chose diagnostics over. The engine already
self-pins its own widget chrome (PageHost tab bar, Button, TextInput,
Checkbox, Badge, Separator, Tabs) and ships the debug zero-collapse
diagnostic whose message names the recipe.

The REAL engine ask this incident surfaced is the FUSION class — see
finding 1030 (zero-area rects still paint). Additional detail from the
round-3 verification: the pre-fix header crush had a largest-remainder
rounding threshold (survived at page-content minimum ≤ ~40 rows,
crushed at ≥ ~50 at 100x24) — which is exactly why light-fixture
reviews never caught it and the first real gateway payload did.
