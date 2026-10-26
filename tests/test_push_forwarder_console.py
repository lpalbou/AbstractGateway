"""My email → Notifications → Push forwarder (admins; framework backlog 1049), web console: the card
sits beside Devices, says the gateway-wide address in words (Built-in forwarder | Off | the URL,
"set by the launch flag for this run" when the flag wins), and its Edit offers the three choices and
says what the forwarder answered. Script cut from the served page, driven in a node VM."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest
from node_requirement import require_node

pytestmark = pytest.mark.basic


def _slice(html: str, start: str, end: str) -> str:
    a = html.index(start)
    return html[a : html.index(end, a)]


def test_the_card_sits_beside_devices_with_three_choices() -> None:
    from abstractgateway.console import gateway_console_html

    html = gateway_console_html()
    pane = _slice(html, 'id="my-email-pane-notifications"', 'id="my-email-pane-agents"')
    assert pane.index('id="my-push-devices"') < pane.index('id="push-forwarder"')
    card = _slice(pane, '<section id="push-forwarder"', "</section>")
    assert ">Push forwarder</h3>" in card and 'id="push-forwarder-edit"' in card
    options = _slice(card, '<select id="push-forwarder-mode">', "</select>")
    assert [o.split(">")[1] for o in options.split("<option ")[1:]] == ["Built-in forwarder</option", "Off</option", "Your own forwarder</option"]
    assert "loadPushForwarder();" in html and "function renderPushForwarder" in html


HARNESS = r"""
const vm = require('vm');
const els = {};
class El { constructor(id) { this.id = id; this.hidden = false; this.innerHTML = ""; this.textContent = ""; this.value = ""; this.disabled = false; } }
const $ = (id) => els[id] || (els[id] = new El(id));
const ctx = {
  $, els, console, calls: [],
  emailErrorText: (e) => String(e && e.message || e),
  consoleSeamFailure: (e) => { throw e; },
  own: true, state: { principal: { admin: true } }, answer: null, putAnswer: null, putFail: null,
};
ctx.mbxOwnPage = () => ctx.own;
ctx.api = async (path, opts) => {
  if (path !== "/api/gateway/push/settings") throw new Error("wrong path " + path);
  ctx.calls.push([(opts && opts.method) || "GET", opts && opts.body ? JSON.parse(opts.body) : null]);
  if (opts && opts.method === "PUT") { if (ctx.putFail) throw new Error(ctx.putFail); return ctx.putAnswer; }
  return ctx.answer;
};
vm.createContext(ctx);
vm.runInContext(JS, ctx);
const row = () => [els["push-forwarder-value"].textContent, els["push-forwarder-note"].hidden ? "" : els["push-forwarder-note"].textContent];
(async () => {
  const out = {};
  ctx.answer = { forwarder: { mode: "default", url: null, effective: null, source: "packaged", packaged: null } };
  await vm.runInContext("loadPushForwarder()", ctx);
  out.builtin = row();
  ctx.answer = { forwarder: { mode: "off", url: null, effective: null, source: "setting", packaged: null } };
  await vm.runInContext("loadPushForwarder()", ctx);
  out.off = row();
  ctx.answer = { forwarder: { mode: "url", url: "http://127.0.0.1:8095", effective: "http://127.0.0.1:8095", source: "setting", packaged: null } };
  await vm.runInContext("loadPushForwarder()", ctx);
  out.url = row();
  ctx.answer = { forwarder: { mode: "off", url: null, effective: "https://push.example.org", source: "flag", packaged: null } };
  await vm.runInContext("loadPushForwarder()", ctx);
  out.flag = [row(), els["push-forwarder-saved"].textContent];

  // Edit: the three choices, the address only for "Your own forwarder".
  els["push-forwarder-edit"].onclick();
  out.formOpen = [els["push-forwarder-form"].hidden, els["push-forwarder-mode"].value, els["push-forwarder-url-field"].hidden];
  els["push-forwarder-mode"].value = "url"; els["push-forwarder-mode"].onchange();
  out.urlShown = !els["push-forwarder-url-field"].hidden;
  els["push-forwarder-url"].value = " http://push.example.org ";
  ctx.putFail = "Use https:// for a forwarder on another machine";
  await els["push-forwarder-save"].onclick();
  out.refused = [els["push-forwarder-error"].textContent, els["push-forwarder-error"].hidden, els["push-forwarder-form"].hidden];
  ctx.putFail = null;
  els["push-forwarder-url"].value = "http://127.0.0.1:8095";
  ctx.putAnswer = { forwarder: { mode: "url", url: "http://127.0.0.1:8095", effective: "http://127.0.0.1:8095", source: "setting", packaged: null },
                    probe: { reachable: true, topics: ["ai.abstractframework.assistant"], reason: null } };
  await els["push-forwarder-save"].onclick();
  out.saved = [row(), els["push-forwarder-result"].textContent, els["push-forwarder-form"].hidden];
  ctx.putAnswer = { forwarder: { mode: "url", url: "http://127.0.0.1:9", effective: "http://127.0.0.1:9", source: "setting", packaged: null },
                    probe: { reachable: false, topics: [], reason: "Could not reach it (ConnectError: refused)." } };
  els["push-forwarder-edit"].onclick();
  await els["push-forwarder-save"].onclick();
  out.unreachable = els["push-forwarder-result"].textContent;
  ctx.putAnswer = { forwarder: { mode: "off", url: null, effective: null, source: "setting", packaged: null } };
  els["push-forwarder-edit"].onclick();
  els["push-forwarder-mode"].value = "off";
  await els["push-forwarder-save"].onclick();
  out.offSaved = els["push-forwarder-result"].textContent;
  out.puts = ctx.calls.filter((c) => c[0] === "PUT").map((c) => c[1]);

  ctx.state = { principal: { admin: false } };
  await vm.runInContext("loadPushForwarder()", ctx);
  out.memberHidden = els["push-forwarder"].hidden;
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_row_words_edit_save_and_probe_answers() -> None:
    from abstractgateway.console_push import PUSH_DEVICES_JS

    node = require_node()
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "h.js"
        path.write_text(f"const JS = {json.dumps(PUSH_DEVICES_JS)};\n" + HARNESS, encoding="utf-8")
        proc = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["builtin"] == ["Built-in forwarder", "· none in this release yet, so no alerts are sent"]
    assert out["off"] == ["Off", ""]
    assert out["url"] == ["http://127.0.0.1:8095", ""]
    assert out["flag"][0] == ["https://push.example.org", "· set by the launch flag for this run"]
    assert out["flag"][1] == "Your saved choice (Off) applies when the gateway starts without the flag."
    assert out["formOpen"] == [False, "off", True] and out["urlShown"] is True
    assert out["refused"] == ["Use https:// for a forwarder on another machine", False, False]
    assert out["saved"] == [["http://127.0.0.1:8095", ""], "Saved. The forwarder answered: it pushes to ai.abstractframework.assistant.", True]
    assert out["unreachable"] == "Saved, but the forwarder could not be reached: Could not reach it (ConnectError: refused)."
    assert out["offSaved"] == "Saved."
    assert out["puts"] == [{"mode": "url", "url": "http://push.example.org"}, {"mode": "url", "url": "http://127.0.0.1:8095"},
                           {"mode": "url", "url": "http://127.0.0.1:8095"}, {"mode": "off"}]
    assert out["memberHidden"] is True
