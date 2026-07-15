"""Console themes = the abstractuic kit's themes, generated + drift-pinned
(uic card 0023; operator catch 2026-07-15: the console offered a hand-copied
6-theme subset while the kit serves 21).

The copy is a packaging necessity (the console is served HTML from Python;
wheels carry no kit checkout) — these pins make it impossible for the copy
to rot silently: the drift test regenerates from the kit source and fails
loud on any divergence, and the render pins prove the served page actually
carries every kit theme.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.basic

from abstractgateway import console_theme_sync  # noqa: E402
from abstractgateway.console import gateway_console_html  # noqa: E402
from abstractgateway.console_themes import (  # noqa: E402
    KIT_LIGHT_THEME_IDS,
    KIT_THEME_CSS,
    KIT_THEME_SPECS,
)


def test_generated_module_matches_the_kit_source_exactly() -> None:
    """THE DRIFT PIN: regenerate from the kit checkout and compare byte-for-
    byte. Skips honestly when the kit source is absent (wheel/CI without the
    monorepo) — in the monorepo this is the belt that catches a kit-side
    theme addition/rename before an operator does."""
    kit_src = console_theme_sync.locate_kit_src()
    if kit_src is None:
        pytest.skip("abstractuic kit source not present (non-monorepo checkout)")
    regenerated = console_theme_sync.generate_console_themes_module(kit_src)
    current = (Path(console_theme_sync.__file__).parent / console_theme_sync.GENERATED_MODULE).read_text(
        encoding="utf-8"
    )
    assert current == regenerated, (
        "console_themes.py is STALE vs the abstractuic kit source — run "
        "`python -m abstractgateway.console_theme_sync` and re-verify the console"
    )


def test_every_kit_theme_is_offered_and_styled() -> None:
    """The list and the CSS must cover each other: a listed theme without a
    token block would be a lying picker (class applied, no styles); 'dark'
    is the :root default and is the only spec without a class block."""
    ids = [s["id"] for s in KIT_THEME_SPECS]
    assert len(ids) >= 20, f"kit serves 21 themes; generated list has {len(ids)}"
    assert len(set(ids)) == len(ids)

    css_ids = set(re.findall(r":root\.theme-([a-z0-9-]+)", KIT_THEME_CSS))
    missing = [i for i in ids if i != "dark" and i not in css_ids]
    assert not missing, f"listed themes without CSS blocks: {missing}"

    # Light group coherence: every light spec rides the light-group block
    # (color-scheme: light) so form controls/scrollbars flip polarity.
    for light_id in KIT_LIGHT_THEME_IDS:
        assert light_id in css_ids, f"light theme {light_id} has no CSS block"
    assert "color-scheme: light" in KIT_THEME_CSS


def test_served_console_carries_all_kit_themes() -> None:
    html = gateway_console_html()
    assert "/*__KIT_THEME_CSS__*/" not in html and "__KIT_THEME_SPECS_JSON__" not in html

    # The dropdown source: the spliced specs JSON, verbatim.
    assert json.dumps(KIT_THEME_SPECS, ensure_ascii=False) in html

    # Every non-default theme's token block is in the served CSS.
    for spec in KIT_THEME_SPECS:
        if spec["id"] == "dark":
            continue
        assert f":root.theme-{spec['id']}" in html, spec["id"]

    # The old hand-tuned fork is gone: no console-local per-theme overrides
    # of the alias layer (the kit block + derived aliases are the whole story).
    assert "--button: #3659b8" not in html  # tokyo-night's hand-tuned button

    # Grouped dropdown renders both kit groups.
    assert 'group.label = groupName === "dark" ? "Dark" : "Light"' in html


def test_console_aliases_derive_instead_of_hand_tuning() -> None:
    """The per-theme console look derives from kit tokens via color-mix at
    the alias layer — hand-tuned per-theme console values were the fork's
    other half and must not come back."""
    html = gateway_console_html()
    assert "--panel-2: color-mix(in srgb, var(--bg-secondary) 92%, black)" in html
    assert "--danger-2: color-mix(in srgb, var(--error) 76%, black)" in html
