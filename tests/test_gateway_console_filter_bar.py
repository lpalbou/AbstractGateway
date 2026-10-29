"""The Models page's filter block is the kit's AfFilterBar (abstractuic ui-kit backlog 0074).

Operator design finding (2026-10-29): the Models page's filters "look bad". The ruled shape,
implemented in the kit and used by every place the console renders catalog filters:

- ONE compact, aligned filter bar from the kit (``af-filterbar``);
- one chip size (the kit chip ``af-chip--button af-filterbar__chip``), counts in muted small text;
- one row per facet, the label in a fixed left column, the chips wrapping in the second column;
- the search field and "Fits this computer" on the SAME first line;
- no stray toggle: no "Every quant" switch on any tab (operator ruling, AbstractGateway #79);
  "Fits this computer" is the bar's one toggle.

Places: the Models tab (Catalog and Hugging Face modes) and the setup guide's model catalog
(``mountModelCatalog("first-run", ...)`` in console.py's loadFirstRunModel). The guide's Advanced
default-models table has no filters.

The console vendors a RELEASED kit (console_islands.py). Until a kit release carries the bar, the
console ports the kit's markup functions verbatim and serves a verbatim copy of the kit's CSS
block; these tests pin both against the kit checkout and fail the day the vendored kit carries the
block (so the copies are deleted then).
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

import pytest
from node_requirement import require_node

from abstractgateway import console_catalog, console_islands_sync
from abstractgateway.console import gateway_console_html
from abstractgateway.console_catalog import CATALOG_JS
from abstractgateway.console_islands import ISLANDS_CSS
from abstractgateway.console_ui import CONSOLE_UI_CSS
from test_gateway_console_catalog import _catalog, _hub_page, _run
from test_gateway_console_first_run import _run as _run_first_run

pytestmark = pytest.mark.basic

KIT_CHIP_CLASS = "af-chip af-chip--neutral af-chip--button af-filterbar__chip"
LEGACY = ("mc-chip", "mc-group", "mc-bar", "mc-filters", "mc-mode", "ui-switch")


def _check_kit_bar(html: str, where: str) -> None:
    """The kit filter bar, as the operator ruled it, in one rendered catalog view."""
    assert html.count('<div class="af-filterbar af-filterbar--sticky mc-filterbar">') == 1, where
    lead = html[html.index('<div class="af-filterbar__lead"') : html.index('<div class="af-filterbar__facets"')]
    assert lead.startswith('<div class="af-filterbar__lead" role="search" aria-label="Filter the model catalog">'), where
    assert '<div class="af-filterbar__scope" role="group" aria-label="Search in"' in lead, where
    assert 'class="af-filterbar__scope-btn" data-af-scope="catalog"' in lead and 'data-af-scope="hf"' in lead, where
    # The search field and "Fits this computer" share the ONE first-line element; it holds no other toggle.
    line = re.search(r'<div class="af-filterbar__line">(.*?)</div>', lead, flags=re.S).group(1)
    assert line.startswith('<input type="search" class="af-filterbar__search" data-mc-search="1"'), (where, line)
    assert '<span class="af-filterbar__toggles"><label class="af-switch af-switch--native af-switch--sm">' in line, (where, line)
    assert 'data-mc-fits="1"' in line and "Fits this computer" in line, (where, line)
    assert line.count('role="switch"') == 1 and lead.count('role="switch"') == 1, (where, lead)
    # One row per facet: label first, in its own element (the fixed column), then the chips column.
    facets = html[html.index('<div class="af-filterbar__facets"') :]
    assert re.findall(r'data-af-facet-row="(\w+)"', facets) == ["quant", "provider", "cap", "status"], where
    for facet, label in (("quant", "Quantization"), ("provider", "Provider"), ("cap", "Capability"), ("status", "Status")):
        row = f'role="group" aria-label="{label}" data-af-facet-row="{facet}"><span class="af-filterbar__label" aria-hidden="true">{label}</span><div class="af-filterbar__chips">'
        assert row in facets, (where, facet)
    # Every chip is the kit chip at the filter size; counts are the muted count element.
    chips = re.findall(r'<button type="button" class="([^"]*)" data-af-facet=', facets)
    assert chips and {c.replace(" af-filterbar__chip--zero", "") for c in chips} == {KIT_CHIP_CLASS}, (where, set(chips))
    assert re.search(r'<span class="af-chip__label">[^<]+</span><span class="af-filterbar__count" aria-hidden="true">\d+</span>', facets), where
    # No "Every quant" switch on any tab (operator ruling, AbstractGateway #79): one toggle in all.
    assert "data-mc-every" not in html and "af-filterbar__trailing" not in html, where
    assert html.count('role="switch"') == 1, where
    classes = {c for attr in re.findall(r'class="([^"]*)"', html) for c in attr.split()}
    assert not classes & set(LEGACY), (where, classes & set(LEGACY))


def test_models_tab_catalog_mode_is_the_kit_filter_bar() -> None:
    out = _run(_catalog(10), "unloadable")
    _check_kit_bar(out["html"], "Models tab, Catalog")


def test_models_tab_hugging_face_mode_is_the_kit_filter_bar_without_every_quant() -> None:
    hub = _hub_page(0, 3, page=1, more=False)
    out = _run(_catalog(10), "hf", "#catalog?hf=smol%20lm", hub=hub)
    assert 'data-af-scope="hf" aria-pressed="true"' in out["html"]
    _check_kit_bar(out["html"], "Models tab, Hugging Face")


def test_setup_guide_catalog_and_models_tab_render_the_kit_filter_bar() -> None:
    """The served page end to end: the guide's model step (mountModelCatalog("first-run", ...)
    from loadFirstRunModel) and the Models tab draw the SAME kit bar."""
    code = "agclaim_" + "F" * 43
    result = _run_first_run({"name": "claim", "hash": f"#claim={code}", "code": code, "completed": False, "coreStub": True,
                             "expectMount": True, "dumpCatalogs": True})
    assert result.returncode == 0, result.stderr + result.stdout
    dumps = dict(re.findall(r"^CATALOG (guide|tab) (.*)$", result.stdout, flags=re.M))
    assert set(dumps) == {"guide", "tab"}, result.stdout[-2000:]
    guide, tab = json.loads(dumps["guide"]), json.loads(dumps["tab"])
    _check_kit_bar(guide, "setup guide")
    assert 'data-mc-fits="1" checked' in guide  # the guide presets "Fits this computer"
    _check_kit_bar(tab, "Models tab (served page)")


def test_touch_floor_leaves_the_kit_pills_their_geometry() -> None:
    """On touch the console raises every button to 44 px; the kit's pills (chips, the scope
    switch) keep their 28 px geometry and get the kit's invisible 44 px hit area instead."""
    coarse = CONSOLE_UI_CSS[CONSOLE_UI_CSS.index("@media (pointer: coarse) {") :]
    floor = coarse[: coarse.index("{ min-height: var(--tap-min, 44px) !important; }")]
    selectors = [s.strip() for s in floor[len("@media (pointer: coarse) {") :].split(",")]
    assert "button" not in selectors, selectors
    assert "button:not(.af-chip--button):not(.af-filterbar__scope-btn)" in selectors, selectors


def _block(css: str) -> str | None:
    begin, end = css.find("/* af-filterbar:begin"), css.find("/* af-filterbar:end */")
    return css[begin : end + len("/* af-filterbar:end */")] if begin >= 0 and end > begin else None


def test_the_filter_bar_comes_from_the_vendored_kit_only() -> None:
    """The console holds NO copy of the kit's filter bar: its CSS is the vendored ISLANDS_CSS block
    (served once, before the catalog's own placement rules) and its markup is the bundle's
    filterBarFacetsHtml / filterBarScopeButtonsHtml. Red the day a copy comes back."""
    assert ".af-filterbar__facet {" in ISLANDS_CSS, "the vendored kit (console_islands.py) must carry the af-filterbar block"
    assert not hasattr(console_catalog, "KIT_FILTER_BAR_CSS"), "a console copy of the kit's af-filterbar CSS came back"
    assert "AF_FILTER_ESC" not in CATALOG_JS and "function afFilterChipHtml" not in CATALOG_JS, "a console port of the kit's filter-bar markup came back"
    assert "afFilterKit().filterBarFacetsHtml(" in CATALOG_JS and "afFilterKit().filterBarScopeButtonsHtml(" in CATALOG_JS
    html = gateway_console_html()
    base = ".af-filterbar__facet {\n  display: grid;"
    assert html.count(base) == 1, "the kit filter bar CSS must be served exactly once (from the vendored kit)"
    assert html.index(base) < html.index(".mc-filterbar > .af-filterbar__facets {") < html.index(".mc-card {")


def test_a_bundle_without_the_filter_bar_says_so() -> None:
    """A vendored bundle without filterBarFacetsHtml is a vendoring defect: the Models page fails
    loudly instead of drawing some other bar."""
    out = _run(_catalog(3), "unloadable", kit=False)
    assert "has no filterBarFacetsHtml" in out.get("error", ""), out
