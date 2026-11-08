"""The web console's Keep-awake switch (backlog 1149): beside Start at login on the Gateway
card, a role=switch labelled "Keep the Mac awake while the gateway runs", pressed = on, rendered
from GET /host/runner `keep_awake`; a click PUTs /host/keep-awake and the row re-renders from a
fresh GET (the read-back, never the wish). The real functions are cut out of the served console
and driven in a node VM with a fake `$`, `api` and `state`.
"""

from __future__ import annotations

import json
import subprocess
import tempfile

import pytest
from node_requirement import require_node

pytestmark = pytest.mark.basic


def _html() -> str:
    from abstractgateway.console import gateway_console_html

    return gateway_console_html()


def _functions(html: str) -> str:
    start = html.index("function renderKeepAwake(ka, error) {")
    end = html.index("function renderGatewayHost(runner, tray) {")
    return html[start:end]


def test_markup_and_wiring_beside_start_at_login() -> None:
    html = _html()
    for needle in ('id="gateway-host-awake-row"', 'id="gateway-host-awake-text"', 'id="gateway-host-awake-toggle"',
                   'aria-label="Keep the Mac awake while the gateway runs"',
                   '<span class="af-switch__label">Keep the Mac awake while the gateway runs</span>'):
        assert needle in html, needle
    # Right beside Start at login, same row style, same switch class.
    login = html.index('id="gateway-host-login-row"')
    awake = html.index('id="gateway-host-awake-row"')
    assert 0 < awake - login < 1200, "the keep-awake row follows the Start-at-login row"
    assert html.count('class="af-switch af-switch--sm kv-switch hidden" aria-checked="false" aria-label="Keep the Mac awake') == 1
    assert '$("gateway-host-awake-toggle").onclick = toggleKeepAwake;' in html
    assert '$("gateway-host-awake-row").classList.remove("hidden");' in html
    assert "if (runner) renderKeepAwake(runner.keep_awake || null);" in html


HARNESS = r"""
const vm = require("vm");
const scenario = JSON.parse(process.argv[2]);
const fns = require("fs").readFileSync(process.argv[3], "utf8");
const els = {};
const make = (id) => { els[id] = { id, textContent: "", className: "", title: "", disabled: false, hidden: true, attrs: {},
  setAttribute(n, v) { this.attrs[n] = String(v); }, getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; }, removeAttribute(n) { delete this.attrs[n]; },
  classList: { toggle(n, f) { if (n === "hidden") els[id].hidden = Boolean(f); }, add(n) { if (n === "hidden") els[id].hidden = true; }, remove(n) { if (n === "hidden") els[id].hidden = false; } } }; };
const $ = (id) => els[id] || null;
["gateway-host-awake-text", "gateway-host-awake-toggle", "gateway-host-message"].forEach(make);
let server = JSON.parse(JSON.stringify(scenario.initial));
const calls = [];
async function api(path, opts = {}) {
  calls.push([opts.method || "GET", path, opts.body ? JSON.parse(opts.body) : null]);
  if ((opts.method || "GET") === "PUT") {
    if (scenario.put_error) { const e = new Error(scenario.put_error); e.status = 403; throw e; }
    const body = JSON.parse(opts.body);
    if (server.source !== "flag") { server.value = body.enabled; server.source = "stored"; server.held = body.enabled && server.platform_supported; }
    return { keep_awake: server };
  }
  return { ok: true, paused: false, keep_awake: JSON.parse(JSON.stringify(server)) };
}
const state = { principal: { admin: scenario.admin !== false }, hostRunner: { keep_awake: JSON.parse(JSON.stringify(server)) } };
function _gwMsg(text, kind) { $("gateway-host-message").textContent = text || ""; $("gateway-host-message").className = kind || ""; }
const ctx = vm.createContext({ $, api, state, _gwMsg, console });
vm.runInContext(fns + "\n;this.render = renderKeepAwake; this.toggle = toggleKeepAwake;", ctx);
const snap = () => ({ text: $("gateway-host-awake-text").textContent, pressed: $("gateway-host-awake-toggle").getAttribute("aria-checked"), hidden: $("gateway-host-awake-toggle").hidden, disabled: $("gateway-host-awake-toggle").disabled });
(async () => {
  ctx.render(state.hostRunner.keep_awake);
  const before = snap();
  if (scenario.click) await ctx.toggle();
  console.log(JSON.stringify({ before, after: snap(), calls, msg: $("gateway-host-message").textContent }));
})().catch((e) => { console.error(e); process.exit(1); });
"""


def _drive(scenario: dict) -> dict:
    node = require_node()
    with tempfile.TemporaryDirectory() as d:
        fns, harness = f"{d}/fns.js", f"{d}/harness.js"
        open(fns, "w").write(_functions(_html()))
        open(harness, "w").write(HARNESS)
        proc = subprocess.run([node, harness, json.dumps(scenario), fns], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _mac(value: bool, source: str = "stored", **over) -> dict:
    ka = {"value": value, "source": source, "held": value, "platform_supported": True}
    ka.update(over)
    return ka


def test_stored_on_renders_pressed_and_a_click_turns_it_off_verified_by_get() -> None:
    out = _drive({"initial": _mac(True), "click": True})
    assert out["before"]["pressed"] == "true" and out["before"]["hidden"] is False and out["before"]["disabled"] is False
    assert out["before"]["text"].startswith("On — this Mac does not idle-sleep while the gateway runs")
    assert out["calls"][0] == ["PUT", "/api/gateway/host/keep-awake", {"enabled": False}]
    assert out["calls"][1][:2] == ["GET", "/api/gateway/host/runner"], "verified by GET after the change"
    assert out["after"]["pressed"] == "false" and out["after"]["text"].startswith("Off — this Mac may sleep")
    assert out["msg"] == ""


def test_stored_off_renders_unpressed_and_a_click_turns_it_on() -> None:
    out = _drive({"initial": _mac(False), "click": True})
    assert out["before"]["pressed"] == "false"
    assert out["after"]["pressed"] == "true" and "does not idle-sleep" in out["after"]["text"]


def test_non_macos_says_the_platform_has_no_such_assertion_and_still_switches() -> None:
    out = _drive({"initial": _mac(True, held=False, platform_supported=False), "click": True})
    assert "this system has no such assertion" in out["before"]["text"] and out["before"]["pressed"] == "true"
    assert out["after"]["pressed"] == "false" and "no such assertion" in out["after"]["text"]


def test_a_launch_flag_pins_the_switch_and_says_so() -> None:
    out = _drive({"initial": _mac(False, source="flag", stored_value=True), "click": True})
    assert out["before"]["disabled"] is True and "set by --keep-awake for this run" in out["before"]["text"]
    assert "saved: on" in out["before"]["text"]
    assert out["calls"] == [], "a disabled switch never PUTs"


def test_a_refused_change_is_said_and_the_state_re_read() -> None:
    out = _drive({"initial": _mac(True), "click": True, "put_error": "admin required"})
    assert out["msg"].startswith("Keep awake was not changed: admin required")
    assert out["after"]["pressed"] == "true", "the read-back, not the wish"


def test_a_non_admin_sees_the_state_but_no_switch() -> None:
    out = _drive({"initial": _mac(True), "admin": False})
    assert out["before"]["hidden"] is True and out["before"]["text"].startswith("On")
