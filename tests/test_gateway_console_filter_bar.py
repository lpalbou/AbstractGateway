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
from abstractgateway.console_catalog import CATALOG_JS, KIT_FILTER_BAR_CSS
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


def test_the_page_serves_the_kit_filter_bar_css_until_the_vendored_kit_carries_it() -> None:
    if ".af-filterbar__facet" in ISLANDS_CSS:
        pytest.fail("the vendored kit (console_islands.py) now carries the af-filterbar block: delete KIT_FILTER_BAR_CSS in "
                    "console_catalog.py, its splice in console.py and the markup port (afFilter* in CATALOG_JS); use "
                    "the islands' filterBarFacetsHtml / filterBarScopeButtonsHtml")
    assert _block(KIT_FILTER_BAR_CSS) == KIT_FILTER_BAR_CSS.strip(), "KIT_FILTER_BAR_CSS must be the kit block, markers included"
    html = gateway_console_html()
    # Served once, comments stripped, before the catalog's own placement rules (which override it).
    base = ".af-filterbar__facet {\n  display: grid;"
    assert html.count(base) == 1
    assert html.index(base) < html.index(".mc-filterbar > .af-filterbar__facets {") < html.index(".mc-card {")


def _kit_dir() -> Path:
    kit = console_islands_sync.locate_kit()
    if kit is None:
        pytest.skip("abstractuic ui-kit not present (non-monorepo checkout)")
    return kit


def test_interim_css_is_the_kit_block_verbatim() -> None:
    kit = _kit_dir()
    block = _block((kit / "src" / "theme.css").read_text(encoding="utf-8"))
    if block is None:
        pytest.skip(f"{kit} has no af-filterbar block yet (the kit PR is not merged there): set "
                    "ABSTRACTUIC_SRC=<abstractuic checkout with backlog 0074>/ui-kit/src to run this pin")
    assert KIT_FILTER_BAR_CSS.strip() == block, "KIT_FILTER_BAR_CSS drifted from the kit's af-filterbar block: copy it again"


_PORT_PROBE = r"""
import vm from "node:vm";
const kit = await import(process.argv[2]);
const ctx = vm.createContext({});
vm.runInContext(process.argv[3], ctx);
const facets = [
  { id: "quant", label: "Quantization", value: "8bit", options: [{ value: "all", label: "All" }, { value: "4bit", label: "4-bit", count: 186 }, { value: "8bit", label: "8-bit", count: 128 }, { value: "other", label: "Other", count: 0 }] },
  { id: "status", label: "Status", value: "all", disabled: true, options: [{ value: "all", label: "All" }, { value: "x", label: "X", count: 2, title: "it's" }] },
  { id: 'q"x', label: "<b>&</b>", value: "a", options: [{ value: 'a"b', label: "<i>", disabled: true }] },
];
const trailing = { quant: "<span>switch</span>" };
const scope = [{ value: "catalog", label: "Catalog" }, { value: "hf", label: "Hugging <Face>" }];
console.log(JSON.stringify({
  kit: [kit.afFilterFacetsHtml(facets, trailing), kit.afFilterFacetsHtml(facets), kit.afFilterScopeButtonsHtml(scope, "hf")],
  port: [ctx.afFilterFacetsHtml(facets, trailing), ctx.afFilterFacetsHtml(facets), ctx.afFilterScopeButtonsHtml(scope, "hf")],
}));
"""


def test_console_markup_port_is_the_kit_core() -> None:
    """The console's afFilter* functions produce byte-for-byte the kit core's markup."""
    kit = _kit_dir()
    core = kit / "src" / "af_filter_bar_core.ts"
    if not core.is_file():
        pytest.skip(f"{core} is missing (the kit PR is not merged there): set ABSTRACTUIC_SRC=<abstractuic checkout with backlog 0074>/ui-kit/src")
    node = require_node()
    start = CATALOG_JS.index("    const AF_FILTER_ESC = ")
    end = CATALOG_JS.index("    // The kit's native switch")
    port = CATALOG_JS[start:end]
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", encoding="utf-8", delete=False) as f:
        f.write(_PORT_PROBE)
        probe = f.name
    r = subprocess.run([node, "--experimental-strip-types", "--no-warnings", probe, str(core), port], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout.strip().splitlines()[-1])
    assert got["port"] == got["kit"]
    assert "&lt;i&gt;" in got["kit"][1] and 'disabled=""' in got["kit"][1]
    assert console_catalog.KIT_FILTER_BAR_CSS  # the port and the CSS copy go together
