"""The web console's update panel (0.7.2): it renders the gateway's `update` view, and a
refused start (409: the installer changed, one already runs, check again first) drops the
stale action like the tray does, so the next step is a new check, never the old offer.

The real functions are cut out of the served console and driven in a node VM with a fake
`$`, `api` and `confirmAction`.
"""

from __future__ import annotations

import json
import subprocess
import tempfile

import pytest
from node_requirement import require_node

pytestmark = pytest.mark.basic


def _functions() -> str:
    from abstractgateway.console import gateway_console_html

    html = gateway_console_html()
    render = html[html.index("function renderGatewayUpdate(upd) {"):html.index("async function loadGatewayHost() {")]
    start = html[html.index("async function startGatewayUpdate() {"):html.index("function _pollGatewayUpdate() {")]
    return render + "\n" + start


HARNESS = r"""
const vm = require("vm");
const scenario = JSON.parse(process.argv[2]);
const fns = require("fs").readFileSync(process.argv[3], "utf8");
const els = {};
const make = (id) => { els[id] = { id, textContent: "", className: "", disabled: false, hidden: false, open: false,
  classList: { toggle(n, f) { if (n === "hidden") els[id].hidden = Boolean(f); }, add(n) { if (n === "hidden") els[id].hidden = true; }, remove(n) { if (n === "hidden") els[id].hidden = false; } } }; };
const $ = (id) => els[id] || null;
["gateway-host-version", "gateway-host-update-start", "gateway-host-update-hint", "gateway-host-update-log-box",
 "gateway-host-update-log-summary", "gateway-host-update-log", "gateway-host-message"].forEach(make);
const calls = [];
async function api(path, opts = {}) {
  calls.push([opts.method || "GET", path, opts.body ? JSON.parse(opts.body) : null]);
  const e = new Error(scenario.error); e.status = 409; throw e;
}
const confirms = [];
async function confirmAction(o) { confirms.push(o.message); return true; }
function _gwMsg(text, kind) { $("gateway-host-message").textContent = text || ""; $("gateway-host-message").className = kind || ""; }
function _gwFmtWhen(x) { return String(x); }
function _pollGatewayUpdate() {}
const state = {};
const ctx = vm.createContext({ $, api, confirmAction, _gwMsg, _gwFmtWhen, _pollGatewayUpdate, state, console });
vm.runInContext(fns + "\n;this.render = renderGatewayUpdate; this.start = startGatewayUpdate;", ctx);
(async () => {
  ctx.render(scenario.overview);
  const before = { hidden: $("gateway-host-update-start").hidden, label: $("gateway-host-update-start").textContent };
  await ctx.start();
  const after = { hidden: $("gateway-host-update-start").hidden, action: state.hostUpdate.update.action, msg: $("gateway-host-message").textContent };
  await ctx.start();  // a second click: no confirmation, no request (the offer is gone)
  console.log(JSON.stringify({ before, after, calls, confirms }));
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_a_refused_start_drops_the_stale_action() -> None:
    node = require_node()
    action = {"label": "Update to AbstractFramework 0.6.2", "confirm": "Update runs the AbstractFramework installer…", "command": "/bin/sh install.sh --yes", "source": "https://…/main/scripts/install.sh", "installer_sha256": "ab" * 32}
    overview = {"current": "0.7.1", "install": {"kind": "installer"}, "job": {"state": "idle"},
                "update": {"status": "available", "line": "AbstractFramework 0.6.1 · gateway 0.7.1 · AbstractFramework 0.6.2 available", "hint": "h", "offer": "AbstractFramework 0.6.2", "action": action, "checked_at": "t"}}
    scenario = {"overview": overview, "error": "the installer changed since it was checked"}
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fns, tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as harness:
        fns.write(_functions())
        harness.write(HARNESS)
    proc = subprocess.run([node, harness.name, json.dumps(scenario), fns.name], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["before"] == {"hidden": False, "label": "Update to AbstractFramework 0.6.2"}
    assert out["calls"] == [["POST", "/api/gateway/host/update/start", {"installer_sha256": "ab" * 32}]]
    assert out["confirms"] == ["Update runs the AbstractFramework installer…"], "confirmed once, never re-offered"
    assert out["after"]["hidden"] is True and out["after"]["action"] is None
    assert "the installer changed since it was checked" in out["after"]["msg"] and "Check now" in out["after"]["msg"]
