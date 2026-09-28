"""The web console's Start-at-login switch (wave 2, 2026-09-28): the Gateway
card and the setup guide's Done step render GET /api/gateway/host/start-at-login,
confirm every change, PUT it, and VERIFY by a fresh GET.

The real functions are cut out of the served console and driven in a node VM
with a fake `$`, `api` and `confirmAction`.
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
    start = html.index("const startAtLoginState = {};")
    end = html.index("async function toggleGatewayPause()")
    return html[start:end]


def test_markup_and_wiring() -> None:
    html = _html()
    for needle in ('id="gateway-host-login-row"', 'id="gateway-host-login-text"', 'id="gateway-host-login-toggle"',
                   'id="first-run-login-text"', 'id="first-run-login-toggle"'):
        assert needle in html, needle
    assert '$("gateway-host-login-toggle").onclick = () => toggleStartAtLogin("gateway-host");' in html
    assert '$("first-run-login-toggle").onclick = () => toggleStartAtLogin("first-run");' in html
    assert 'loadStartAtLogin("gateway-host");' in html and 'loadStartAtLogin("first-run");' in html


HARNESS = r"""
const vm = require("vm");
const scenario = JSON.parse(process.argv[2]);
const fns = require("fs").readFileSync(process.argv[3], "utf8");
const els = {};
const make = (id) => { els[id] = { id, textContent: "", className: "", disabled: false, hidden: true,
  classList: { toggle(n, f) { if (n === "hidden") els[id].hidden = Boolean(f); }, add(n) { if (n === "hidden") els[id].hidden = true; }, remove(n) { if (n === "hidden") els[id].hidden = false; } } }; };
// Like getElementById: only what is on the page (the Done step is not rendered here).
const $ = (id) => els[id] || null;
["gateway-host-login-text", "gateway-host-login-toggle", "gateway-host-message", "first-run-message"].forEach(make);
let server = JSON.parse(JSON.stringify(scenario.initial));
const calls = [];
async function api(path, opts = {}) {
  calls.push([opts.method || "GET", path, opts.body ? JSON.parse(opts.body) : null]);
  if ((opts.method || "GET") === "PUT") {
    if (scenario.put_error) { const e = new Error(scenario.put_error); e.status = 409; throw e; }
    const body = JSON.parse(opts.body);
    if (!scenario.put_ignored) { server.enabled = body.enabled; server.state = body.enabled ? "on" : "off"; server.summary = body.enabled ? "On — a LaunchAgent starts the gateway at login" : "Off — nothing starts the gateway at login"; }
    return { ok: true };
  }
  return JSON.parse(JSON.stringify(server));
}
const confirms = [];
async function confirmAction(o) { confirms.push(o); return scenario.confirm; }
function _gwMsg(text, kind) { $("gateway-host-message").textContent = text || ""; $("gateway-host-message").className = kind || ""; }
const ctx = vm.createContext({ $, api, confirmAction, _gwMsg, console });
vm.runInContext(fns + "\n;this.load = loadStartAtLogin; this.toggle = toggleStartAtLogin;", ctx);
(async () => {
  await ctx.load("gateway-host");
  const before = { text: $("gateway-host-login-text").textContent, button: $("gateway-host-login-toggle").textContent, hidden: $("gateway-host-login-toggle").hidden };
  await ctx.toggle("gateway-host");
  console.log(JSON.stringify({ before, after: { text: $("gateway-host-login-text").textContent, button: $("gateway-host-login-toggle").textContent }, calls, confirms, msg: $("gateway-host-message").textContent }));
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


OFF = {"enabled": False, "state": "off", "mechanism": "launchd-agent", "mechanism_label": "a LaunchAgent", "can_change": True,
       "reason": None, "summary": "Off — nothing starts the gateway at login"}


def test_turn_on_is_confirmed_put_and_verified_by_get() -> None:
    out = _drive({"initial": OFF, "confirm": True})
    assert out["before"] == {"text": "Off — Off — nothing starts the gateway at login", "button": "Turn on…", "hidden": False}
    assert "a LaunchAgent" in out["confirms"][0]["message"]
    assert [c[0] for c in out["calls"]] == ["GET", "PUT", "GET"]
    assert out["calls"][1] == ["PUT", "/api/gateway/host/start-at-login", {"enabled": True, "replace_other": False}]
    assert out["after"]["text"].startswith("On — ") and out["after"]["button"] == "Turn off…" and out["msg"] == ""


def test_cancel_changes_nothing() -> None:
    out = _drive({"initial": OFF, "confirm": False})
    assert [c[0] for c in out["calls"]] == ["GET"]


def test_a_change_that_does_not_read_back_is_said() -> None:
    out = _drive({"initial": OFF, "confirm": True, "put_ignored": True})
    assert "did not read back as on" in out["msg"]
    out = _drive({"initial": OFF, "confirm": True, "put_error": "refused"})
    assert out["msg"] == "Start at login was not changed: refused"


def test_cannot_change_shows_the_reason_and_no_button() -> None:
    st = dict(OFF, can_change=False, reason="no systemd user manager answers on this machine and there is no desktop session")
    out = _drive({"initial": st, "confirm": True})
    assert out["before"]["hidden"] is True and "can't be changed here: no systemd user manager" in out["before"]["text"]
    assert [c[0] for c in out["calls"]] == ["GET"] and out["confirms"] == []


def test_another_gateway_is_replaced_only_with_replace_other() -> None:
    st = dict(OFF, state="other", other_data_dir="/srv/other", summary="Registered (a LaunchAgent) for another gateway")
    out = _drive({"initial": st, "confirm": True})
    assert out["before"]["button"] == "Use this gateway…" and "/srv/other" in out["confirms"][0]["message"]
    assert out["calls"][1][2] == {"enabled": True, "replace_other": True}
