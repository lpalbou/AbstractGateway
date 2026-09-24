"""Card 015 (console overhaul wave 3) markup pins.

The card's acceptance criterion: each in-scope item lands with a markup-pin
or render test. These pin the shipped items against the SERVED html so a
regression (or a refactor dropping a recipe) lands red, not silent:

- page-wide inline-SVG icon registry + boot hydration (one glyph = one verb;
  no platform emoji/VS15 dependence);
- chip unification (one shared recipe over the five pill families);
- radius normalization onto the token scale (4/8/10/999);
- re-embed relocated to the Substrate panel behind a danger-zone disclosure;
- table loading rows (a header-only table must never read as broken);
- create-flow staging: dry-run warnings reviewed BEFORE the irreversible act.
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.basic


@pytest.fixture(scope="module")
def html() -> str:
    from abstractgateway.console import gateway_console_html

    return gateway_console_html()


def test_icon_registry_and_hydration(html: str) -> None:
    assert "const ICONS = {" in html, "the page-wide icon registry must exist"
    for name in ("refresh:", "retry:", "gear:", "warn:", "lock:"):
        assert name in html, f"registry must carry the {name[:-1]} icon"
    # Boot hydration swaps unicode fallbacks for registry SVGs (guarded for
    # DOM stubs without querySelectorAll).
    assert 'typeof document.querySelectorAll === "function"' in html
    # Every remaining unicode glyph site carries its hydration class — a new
    # bare glyph span (no icon-* class) would dodge hydration and regress to
    # the platform emoji lottery.
    for glyph, cls in (("↻", "icon-refresh"), ("⚙", "icon-gear"), ("⟳", "icon-retry")):
        for m in re.finditer(re.escape(glyph), html):
            window = html[max(0, m.start() - 220):m.start()]
            assert cls in window or "ICONS." in window, (
                f"glyph {glyph!r} appears without its {cls} hydration class near ...{window[-120:]}"
            )
    # The lock chip and state warn are registry SVGs now, never raw emoji.
    assert "🔒" not in html, "the lock chip must use ICONS.lock, not the emoji"
    assert "⚠ state unreadable" not in html, "the state warn must use ICONS.warn"


def test_chip_unification_single_recipe(html: str) -> None:
    """The five pill families share ONE shape recipe; families keep only
    their state-color deltas (class names preserved by ruling)."""
    assert ".pill, .badge, .state-pill, .entity-chip, .entity-warn-pill, .entity-live-badge {" in html
    # The families must not re-declare the shared shape (border-radius) in
    # their DELTA blocks (a rule whose selector is exactly the one family) —
    # one recipe means one radius source.
    for family in (".entity-live-badge", ".entity-warn-pill"):
        # Anchored at rule start: the shared selector LIST also ends with
        # these names, so an unanchored search matches the shared recipe.
        m = re.search(r"(?m)^\s*" + re.escape(family) + r"\s*\{([^}]*)\}", html)
        assert m, f"{family} delta block missing"
        assert "border-radius" not in m.group(1), f"{family} re-declares the shared shape"


def test_radius_scale_normalized(html: str) -> None:
    """Radii ride the token scale. Documented exceptions: 999px (pill), 50%
    (circle), inherit, and the pc-chat-item 12px (the shared uic transcript
    recipe — snapping it locally would drift from the kit's pen)."""
    offenders = []
    # The abstractuic kit's component CSS (<style id="af-kit-css">, vendored
    # verbatim by console_islands_sync) carries the kit's own radii; this pin
    # is about the console's sheet.
    kit_start = html.index('<style id="af-kit-css">')
    html = html[:kit_start] + html[html.index("</style>", kit_start):]
    for m in re.finditer(r"border-radius:\s*([^;]+);", html):
        value = m.group(1).strip()
        if value.startswith("var(--radius-"):
            continue
        if value in ("999px", "50%", "inherit", "12px"):
            continue
        offenders.append(value)
    assert not offenders, f"off-token radii: {offenders}"


def test_reembed_lives_in_substrate_behind_danger_disclosure(html: str) -> None:
    """Disclosure P0-3 second half: the CRITICAL repair sits beside the mind
    it repairs (Substrate), inside a <details> danger zone — never in
    Lifecycle where it read as a routine state verb."""
    substrate = html.find('id="entity-subpanel-substrate"')
    lifecycle = html.find('id="entity-subpanel-lifecycle"')
    reembed = html.find('id="entity-reembed"')
    assert substrate != -1 and lifecycle != -1 and reembed != -1
    # The reembed button comes after the substrate panel opens and the panel
    # between lifecycle and substrate no longer contains it.
    assert reembed > substrate, "re-embed must live in the Substrate panel"
    lifecycle_block = html[lifecycle:substrate] if lifecycle < substrate else ""
    assert 'id="entity-reembed"' not in lifecycle_block, "re-embed must be gone from Lifecycle"
    # Danger-zone disclosure wraps it.
    disclosure = html.rfind("<details", substrate, reembed)
    assert disclosure != -1, "re-embed must sit behind a <details> disclosure"
    assert "Danger zone" in html[disclosure:reembed]


def test_tables_declare_loading_rows(html: str) -> None:
    assert "function tableLoadingRow(" in html
    # "Measuring data homes…" became "Measuring caches…" when the machine-wide
    # data-homes walk folded into the runtime Cache tab (2026-08-19 redesign).
    for text in ("Measuring caches…", "Loading runs…", "Loading the roster…", "Scanning execution planes…"):
        assert text in html, f"table loading state {text!r} missing"


def test_create_flow_reviews_warnings_before_the_birth(html: str) -> None:
    """Card 015 staging: the dry-run's warnings ride the CONFIRM (review
    before the irreversible act), not only the post-create note."""
    confirm = html.find("Validation warnings (review before summoning)")
    create_call = html.find('await api("/api/gateway/entities", { method: "POST"')
    assert confirm != -1, "the confirm must carry the dry-run warnings"
    assert create_call != -1
    assert confirm < create_call, "warnings must surface before the create call"
