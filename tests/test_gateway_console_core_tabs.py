"""Models / Engines tabs: AbstractCore's screens embedded in the web console.

The Gateway does not re-implement the model browser or the engine installer:
`gateway_console_html()` splices AbstractCore's fragments (through the
`core_config` seam) into two tabs, "Models" (id `catalog`) and "Engines" (id
`engines`), which never collide with "Resources" (id `models`) or "Runtimes".
Pins here:

- the tab ids, their order after the existing tabs, and the panels;
- the fragment html (per kind), css and js each appear EXACTLY once;
- the fragment cannot break the page (`</script>`, braces, `$`, template
  tokens inside the fragment are inert) and every script still parses;
- the mount options (apiBase `/api/gateway`, cliPrefix `abstractgateway`,
  the gateway host name);
- an AbstractCore older than 2.14.0 (or missing) renders an upgrade card in
  both panels, ships no fragment script, and the page still parses.

The mounting itself is driven in a node VM by test_gateway_console_first_run.py.
"""

from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import tempfile

import pytest

from abstractgateway import core_config
from abstractgateway.console import gateway_console_html

pytestmark = pytest.mark.basic


def _all_scripts(html: str) -> list[str]:
    return re.findall(r"<script(?:\s+id=\"[^\"]*\")?>(.*?)</script>", html, flags=re.S)


def _node_check(source: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for JavaScript syntax checking")
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8") as f:
        f.write(source)
        f.flush()
        result = subprocess.run([node, "--check", f.name], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def _core_console_config(html: str) -> dict:
    match = re.search(r"const CORE_CONSOLE = (\{.*?\});\n", html)
    assert match, "CORE_CONSOLE config not spliced"
    return json.loads(match.group(1))


def _require_screens() -> dict:
    support = core_config.core_models_engines_support()
    if not support.get("available"):
        pytest.skip(f"installed AbstractCore has no console screens: {support}")
    return support


def test_tabs_are_appended_with_their_own_ids() -> None:
    html = gateway_console_html()
    for element_id in ("tab-button-catalog", "tab-button-engines", "tab-catalog", "tab-engines", "catalog-core-root", "engines-core-root"):
        assert html.count(f'id="{element_id}"') == 1, element_id
    # The existing tabs keep their ids and meaning.
    for element_id in ("tab-button-models", "tab-models", "tab-button-runtimes", "tab-runtimes"):
        assert html.count(f'id="{element_id}"') == 1, element_id
    assert 'const TABS = ["users", "runtimes", "workflows", "providers", "defaults", "sandbox", "models", "catalog", "engines", "apps", "network"];' in html
    nav = html[html.index('<nav class="shell_nav">') : html.index("</nav>")]
    order = re.findall(r'id="tab-button-([a-z]+)"', nav)
    # Mission L appends Apps (the gateway's apps service) after Engines;
    # mission L2 appends Network (who can reach the gateway) after Apps.
    assert order[-5:] == ["models", "catalog", "engines", "apps", "network"], order
    assert '<span class="shell_nav_label">Models</span>' in nav
    assert '<span class="shell_nav_label">Engines</span>' in nav
    assert 'catalog: ["Models",' in html and 'engines: ["Engines",' in html


def test_fragments_are_spliced_exactly_once() -> None:
    _require_screens()
    html = gateway_console_html()
    models = core_config.core_console_fragment("models")
    engines = core_config.core_console_fragment("engines")
    assert models["js"] == engines["js"] and models["css"] == engines["css"]
    assert html.count(models["html"]) == 1
    assert html.count(engines["html"]) == 1
    assert html.count(models["css"].strip()) == 1
    assert html.count('<script id="abstractcore-console-js">') == 1
    # The screens' script comes BEFORE the console script that mounts it.
    assert html.index('<script id="abstractcore-console-js">') < html.index("const CORE_CONSOLE =")
    # The spliced html sits inside the right panels.
    catalog_panel = html[html.index('id="tab-catalog"') : html.index('id="tab-engines"')]
    assert 'data-acc-kind="models"' in catalog_panel
    engines_panel = html[html.index('id="tab-engines"') : html.index('id="tab-users"')]
    assert 'data-acc-kind="engines"' in engines_panel
    for token in ("__ABSTRACTCORE_", "__CORE_CONSOLE_CONFIG_JSON__", "__KIT_THEME"):
        assert token not in html, token


def test_every_script_parses_including_the_screens() -> None:
    _require_screens()
    html = gateway_console_html()
    scripts = _all_scripts(html)
    assert len(scripts) == 3, "expected the kit islands, the AbstractCore screens script and the console script"
    for source in scripts:
        _node_check(source)


def test_mount_options_name_the_gateway() -> None:
    _require_screens()
    html = gateway_console_html()
    config = _core_console_config(html)
    assert config["available"] is True
    assert config["hostName"] == (socket.gethostname() or "gateway host")
    for needle in (
        'apiBase: "/api/gateway",',
        "request: coreConsoleRequest,",
        "isAdmin: () => !!(state.principal && state.principal.admin),",
        "hostName: CORE_CONSOLE.hostName,",
        'cliPrefix: "abstractgateway",',
        "onJob: coreConsoleOnJob,",
    ):
        assert needle in html, needle
    # The adapter must not proxy the core first-run claim: the gateway has its own.
    assert "/acore/session/claim" not in html


def test_hostile_fragment_content_cannot_break_the_page(monkeypatch) -> None:
    hostile_js = 'window.AbstractCoreConsole = { mount() {} }; const s = "</script><b>x</b>"; const t = `${1} {x} $& __CORE_CONSOLE_CONFIG_JSON__`;'
    hostile_html = '<div class="acc-root" data-acc-kind="{kind}">$1 {braces} /*__KIT_THEME_CSS__*/ <!--__ABSTRACTCORE_ENGINES_HTML__--></div>'

    def fake_fragment(kind):
        return {"html": hostile_html.replace("{kind}", kind), "js": hostile_js, "css": ".acc-root { color: red; } /* </style> */"}

    monkeypatch.setattr(core_config, "core_console_fragment", fake_fragment)
    html = gateway_console_html()
    # Content is inert: tokens inside it are NOT expanded, the script tag is not closed early.
    assert html.count("__CORE_CONSOLE_CONFIG_JSON__") == 1  # only the copy inside the fragment js
    assert html.count("<!--__ABSTRACTCORE_ENGINES_HTML__-->") == 2  # the two copies inside the fragment html
    assert "</style> */" not in html
    scripts = _all_scripts(html)
    assert len(scripts) == 3  # kit islands + AbstractCore screens + console
    for source in scripts:
        _node_check(source)
    assert _core_console_config(html)["available"] is True


def test_older_abstractcore_renders_an_upgrade_card(monkeypatch) -> None:
    def too_old(kind):
        raise core_config.CoreTooOld("The embeddable console screens", "2.13.42")

    monkeypatch.setattr(core_config, "core_console_fragment", too_old)
    monkeypatch.setattr(
        core_config,
        "core_models_engines_support",
        lambda: {"available": False, "abstractcore_version": "2.13.42", "required": "2.14.0", "missing": ["abstractcore.console.web"]},
    )
    html = gateway_console_html()
    assert "abstractcore-console-js" not in html
    assert 'data-acc-kind="models"' not in html
    for panel_id, nxt in (("tab-catalog", "tab-engines"), ("tab-engines", "tab-users")):
        panel = html[html.index(f'id="{panel_id}"') : html.index(f'id="{nxt}"')]
        assert 'data-core-console="unavailable"' in panel
        assert "Models and Engines require abstractcore ≥ 2.14.0" in panel
        assert "abstractcore 2.13.42" in panel
        assert "pip install -U &quot;abstractcore&gt;=2.14.0&quot;" in panel
    config = _core_console_config(html)
    assert config["available"] is False
    assert config["installed"] == "2.13.42"
    scripts = _all_scripts(html)
    assert len(scripts) == 2  # kit islands + console (no AbstractCore screens)
    for source in scripts:
        _node_check(source)


def test_missing_abstractcore_renders_the_card_with_the_reason(monkeypatch) -> None:
    def missing(kind):
        raise RuntimeError("The embeddable console screens needs AbstractCore, which is not installed.")

    monkeypatch.setattr(core_config, "core_console_fragment", missing)
    monkeypatch.setattr(
        core_config,
        "core_models_engines_support",
        lambda: {"available": False, "abstractcore_version": None, "required": "2.14.0", "missing": ["abstractcore"]},
    )
    html = gateway_console_html()
    assert html.count('data-core-console="unavailable"') == 2
    assert "which is not installed" in html
    assert _core_console_config(html)["available"] is False
    _node_check(_all_scripts(html)[0])


def test_apps_cards_pin_one_action_row_and_render_technical_parts_on_demand() -> None:
    """Mission GG (operator, 2026-09-24): every app card is icon + name + pill /
    one line / ONE action row, the rows at the same level across the grid
    (five subgrid rows per card); Stop, Show log, Update, versions and
    commands are RENDERED only with Technical details on, and the switch
    re-renders the cards; no permanent box restates the status pill."""
    from abstractgateway.console_ui import CONSOLE_UI_CSS, CONSOLE_UI_JS

    assert '`<div class="ui-card-grid is-aligned">${apps.map(appCardMarkup).join("")}</div>`' in CONSOLE_UI_JS
    assert ".ui-card-grid.is-aligned > .ui-card { display: grid; grid-row: span 5; grid-template-rows: subgrid;" in CONSOLE_UI_CSS
    assert "try { appRender(); consoleTuiRender(); } catch" in CONSOLE_UI_JS
    assert "const techOn = uiShowAdvanced();" in CONSOLE_UI_JS
    for gone in ("Open it with the Open button", "Also runs in your terminal", "ui-iface"):
        assert gone not in CONSOLE_UI_JS and gone not in CONSOLE_UI_CSS, gone
    html = gateway_console_html()
    assert "Open it with the Open button" not in html
