"""My email → Notifications → Devices (framework backlog 1049), web console: the card sits in the
Notifications tab after the email switches, has nothing to configure, and its script (cut from the
served page, driven in a node VM with a fake DOM) renders one row per device — app, name,
registered, last alert, last error — never a token, hides on an entity's page, and says a failed
read in words."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest
from node_requirement import require_node

pytestmark = pytest.mark.basic


def _html() -> str:
    from abstractgateway.console import gateway_console_html

    return gateway_console_html()


def _slice(html: str, start: str, end: str) -> str:
    a = html.index(start)
    return html[a : html.index(end, a)]


def test_devices_card_is_in_the_notifications_tab_with_nothing_to_configure() -> None:
    html = _html()
    notifications = _slice(html, 'id="my-email-pane-notifications"', 'id="my-email-pane-agents"')
    card = _slice(notifications, '<section id="my-push-devices"', "</section>")
    assert notifications.index('id="my-email-notify-title"') < notifications.index('id="my-push-devices"')
    assert ">Devices</h3>" in card and 'id="my-push-devices-list"' in card
    assert "<button" not in card and "<input" not in card and 'role="switch"' not in card
    assert "loadMyPushDevices();" in html and "function renderMyPushDevices" in html


HARNESS = r"""
const vm = require('vm');
const els = {};
class El { constructor(id) { this.id = id; this.hidden = false; this.innerHTML = ""; this.textContent = ""; } }
const $ = (id) => els[id] || (els[id] = new El(id));
const ctx = {
  $, els, console,
  esc: (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
  emailAgo: (iso) => iso ? "5 min ago" : "",
  emailErrorText: (e) => String(e && e.message || e),
  consoleSeamFailure: (e) => { throw e; },
  own: true, answer: null, fail: null,
};
ctx.mbxOwnPage = () => ctx.own;
ctx.api = async (path) => { if (path !== "/api/gateway/me/push/devices") throw new Error("wrong path " + path); if (ctx.fail) throw new Error(ctx.fail); return ctx.answer; };
vm.createContext(ctx);
vm.runInContext(JS, ctx);
(async () => {
  const out = {};
  ctx.answer = [
    { device_id: "aw1", app: "watch", name: "Wrist", refreshed_at: "2026-10-25T10:00:00+00:00", last_sent_at: "2026-10-25T10:05:00+00:00", last_error: null },
    { device_id: "ap1", app: "phone", name: null, refreshed_at: "2026-10-25T10:00:00+00:00", last_sent_at: null, last_error: "The push relay refused an alert: <BadTopic>" },
  ];
  await vm.runInContext("loadMyPushDevices()", ctx);
  out.list = els["my-push-devices-list"].innerHTML;
  out.state = [els["my-push-devices-state"].textContent, els["my-push-devices-state"].hidden];
  ctx.answer = [];
  await vm.runInContext("loadMyPushDevices()", ctx);
  out.empty = [els["my-push-devices-list"].innerHTML, els["my-push-devices-state"].textContent];
  ctx.fail = "Gateway unreachable";
  await vm.runInContext("loadMyPushDevices()", ctx);
  out.failed = els["my-push-devices-state"].textContent;
  ctx.fail = null; ctx.own = false;
  await vm.runInContext("loadMyPushDevices()", ctx);
  out.entityHidden = els["my-push-devices"].hidden;
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_devices_render_hide_and_fail_in_words() -> None:
    from abstractgateway.console_push import PUSH_DEVICES_JS

    node = require_node()
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "h.js"
        path.write_text(f"const JS = {json.dumps(PUSH_DEVICES_JS)};\n" + HARNESS, encoding="utf-8")
        proc = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "Wrist · Apple Watch" in out["list"] and "Registered 5 min ago · last alert 5 min ago" in out["list"]
    assert ">iPhone<" in out["list"] and "no alert yet" in out["list"]
    assert "The push relay refused an alert: &lt;BadTopic&gt;" in out["list"]
    assert out["state"] == ["", True]
    assert out["empty"][0] == "" and out["empty"][1].startswith("No watch or iPhone yet.")
    assert out["failed"] == "Could not list your devices: Gateway unreachable"
    assert out["entityHidden"] is True
