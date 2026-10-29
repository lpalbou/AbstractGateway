"""The console top bar mounts the kit's ONE account widget (operator ruling 2026-10-29).

Every app and the gateway console show the same top-bar set from the kit
(AbstractUIC ui-kit 0.9.6 `AfTopBarActions`): docs drawer button, Appearance,
About, and one account widget (person icon, name, status light; menu: Signed in
as / Gateway / Copy user id / Disconnect). The console gets it through the
vendored islands bundle by passing `identity: "menu"` and `connection.me` (its
own `GET /api/gateway/me` body) to `mountTopBar`, instead of the former
`island-identity` "Signed in as" text extra beside the pill.

These checks go RED if the console stops passing `identity: "menu"` or
`connection.me`, brings the text extra back, or vendors a kit older than the
one that ships the account widget.
"""

from __future__ import annotations

import json
import subprocess
import tempfile

import pytest
from node_requirement import require_node

from abstractgateway.console import gateway_console_html
from abstractgateway.console_islands import ISLANDS_JS, ISLANDS_PROVENANCE

pytestmark = pytest.mark.basic

ACCOUNT_WIDGET_KIT = (0, 9, 6)
ME_BODY = {
    "ok": True,
    "principal": {"tenant_id": "default", "user_id": "admin", "roles": ["admin"]},
    "auth": {"user_auth_enabled": True},
}


def _slice(html: str, start: str, end: str) -> str:
    i = html.index(start)
    j = html.index(end, i)
    return html[i:j]


def _run_node(script: str, *args: str) -> dict:
    node = require_node()
    out = subprocess.run([node, "-e", script, *args], capture_output=True, text=True, check=True, timeout=60)
    return json.loads(out.stdout.strip().splitlines()[-1])


def _top_bar_props(me: object) -> dict:
    """Run the served console's real topBarIslandProps() under stubs; return its props as JSON."""
    fn = _slice(gateway_console_html(), "function topBarIslandProps() {", "function appearanceIslandProps(")
    script = (
        "const fs = require('node:fs');"
        "const src = fs.readFileSync(process.argv[1], 'utf8');"
        "const me = JSON.parse(process.argv[2]);"
        "const state = { principal: me ? me.principal : null, me: me };"
        "const assistantState = { open: false };"
        "const islands = { phase: me ? 'connected' : 'disconnected', signingOut: false, warming: false };"
        "const noop = () => {};"
        "const $ = () => null;"
        "const toggleAssistant = noop, openAppearance = noop, openGatewayAbout = noop;"
        "const renderIslands = noop, signOut = noop;"
        "const warmingExtra = () => ({ id: 'warming', label: 'warming', hidden: true });"
        "const topBarIslandProps = new Function('state', 'assistantState', 'islands', '$', 'toggleAssistant',"
        " 'openAppearance', 'openGatewayAbout', 'renderIslands', 'signOut', 'warmingExtra',"
        " src + '\\nreturn topBarIslandProps;')(state, assistantState, islands, $, toggleAssistant,"
        " openAppearance, openGatewayAbout, renderIslands, signOut, warmingExtra);"
        "const p = topBarIslandProps();"
        "console.log(JSON.stringify({ props: p, hasDisconnect: typeof p.connection.onDisconnect === 'function' }));"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as f:
        f.write(fn)
        path = f.name
    return _run_node(script, path, json.dumps(me))


def test_console_top_bar_passes_the_account_widget_props() -> None:
    got = _top_bar_props(ME_BODY)
    props = got["props"]
    assert props["identity"] == "menu"
    assert props["connection"]["me"] == ME_BODY
    assert got["hasDisconnect"], "the account menu's Disconnect calls connection.onDisconnect"
    ids = [x.get("id") for x in props.get("extras") or [] if x]
    assert "island-identity" not in ids, "the old 'Signed in as' text extra came back beside the pill"
    assert not [x for x in props.get("extras") or [] if x and "text" in x], "a text extra is not the account widget"
    # The local About (update controls) and the warming extra stay.
    assert "gateway-about-open" in ids and "warming" in ids


def test_signed_out_console_passes_no_identity_body() -> None:
    props = _top_bar_props(None)["props"]
    assert props["identity"] == "menu"
    assert props["connection"]["me"] is None


def test_render_account_holds_the_me_body_and_no_text_identity_remains() -> None:
    html = gateway_console_html()
    render_account = _slice(html, "function renderAccount(me) {", "if (!p) {")
    assert "state.me = p ? me : null;" in render_account
    assert "island-identity" not in html
    assert "islands.identity" not in html


def test_vendored_kit_ships_the_account_widget() -> None:
    version = tuple(int(x) for x in ISLANDS_PROVENANCE["kit_version"].split("."))
    assert version >= ACCOUNT_WIDGET_KIT, ISLANDS_PROVENANCE["kit_version"]
    assert "af-topbar__identity--menu" in ISLANDS_JS
    # The island reads connection.me as a signed-in status on this origin.
    script = (
        "const vm = require('node:vm'); const fs = require('node:fs');"
        "const sb = { console, location: { origin: 'https://gw.example' } }; sb.globalThis = sb; sb.window = sb;"
        "vm.createContext(sb);"
        "vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), sb);"
        "const me = JSON.parse(process.argv[2]);"
        "const f = sb.AfConsoleIslands.islandConnectionStatus;"
        "console.log(JSON.stringify({ on: f({ phase: 'connected', me: me }), off: f({ phase: 'disconnected', me: me }) }));"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as f:
        f.write(ISLANDS_JS)
        bundle = f.name
    got = _run_node(script, bundle, json.dumps(ME_BODY))
    assert got["on"]["has_session"] is True
    assert got["on"]["gateway"]["principal"]["user_id"] == "admin"
    assert got["off"] is None
