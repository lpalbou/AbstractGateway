"""The web console's docs assistant (ADR-0026, operator ruling 2026-09-28).

No client-side history copy and no turn cap: each question starts docs-qa 0.1.1
in the conversation's gateway session with `use_session_history`, the gateway
replays the earlier turns through the runtime's history window, and the drawer
shows the run's `session_history` receipt when earlier messages were not
replayed. New conversation = a new session.

The real drawer functions are cut out of the served console and driven in a
node VM with a fake `$` and `api`.
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
    start = html.index("const ASSISTANT_BUNDLE = ")
    end = html.index("const TAB_TITLES = {")
    return html[start:end]


def test_markup_and_wiring() -> None:
    html = _html()
    assert 'id="assistant-replay"' in html
    assert ">New conversation</button>" in html
    assert '$("assistant-clear").onclick = assistantClear;' in html
    assert "slice(-12)" not in _functions(html)


HARNESS = r"""
const vm = require("vm");
const scenario = JSON.parse(process.argv[2]);
const fns = require("fs").readFileSync(process.argv[3], "utf8");
const els = {};
function make(id) {
  const kids = [];
  els[id] = { id, textContent: "", value: "", hidden: false, disabled: false, scrollTop: 0, scrollHeight: 0, kids,
    classList: { add() {}, remove() {}, toggle() {} },
    append(k) { kids.push(k); }, focus() {} };
  Object.defineProperty(els[id], "textContent", { get() { return this._t || ""; }, set(v) { this._t = v; if (v === "") kids.length = 0; } });
}
["assistant-messages", "assistant-note", "assistant-replay", "assistant-input", "assistant-send"].forEach(make);
els["assistant-replay"].hidden = true;
const $ = (id) => els[id] || null;
const document = { createElement() { return { className: "", textContent: "", classList: { add() {}, remove() {} } }; } };
const calls = [];
let runs = 0;
async function api(path, opts = {}) {
  calls.push([opts.method || "GET", path, opts.body ? JSON.parse(opts.body) : null]);
  if (path === "/api/gateway/docs/corpus") return { app: "AbstractGateway", text: "# docs" };
  if (path === "/api/gateway/runs/start") { runs += 1; return { run_id: "r" + runs }; }
  const i = Number(path.split("/r").pop()) - 1;
  return { status: "completed", output: { response: "answer-" + (i + 1) }, session_history: scenario.receipts[i] };
}
const ctx = vm.createContext({ $, api, document, console, setTimeout: (f) => f(), crypto: { randomUUID: () => "u" + Math.random().toString(16).slice(2) } });
vm.runInContext(fns + "\n;this.submit = assistantSubmit; this.clear = assistantClear; this.state = assistantState;", ctx);
(async () => {
  const out = { sessions: [], replay: [] };
  for (const q of scenario.questions) {
    if (q === "__clear__") { ctx.clear(); out.replay.push({ text: $("assistant-replay").textContent, hidden: $("assistant-replay").hidden }); continue; }
    $("assistant-input").value = q;
    await ctx.submit();
    out.sessions.push(ctx.state.sessionId);
    out.replay.push({ text: $("assistant-replay").textContent, hidden: $("assistant-replay").hidden });
  }
  out.starts = calls.filter((c) => c[1] === "/api/gateway/runs/start").map((c) => c[2]);
  console.log(JSON.stringify(out));
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


def test_questions_send_only_the_question_in_one_session_with_the_session_replay() -> None:
    out = _drive({"questions": ["first?", "second?"], "receipts": [None, {"replayed_messages": 2, "dropped_messages": 0}]})
    first, second = out["starts"]
    for body, q in ((first, "first?"), (second, "second?")):
        assert body["bundle_id"] == "docs-qa" and body["bundle_version"] == "0.1.1" and body["flow_id"] == "docsqa001"
        assert body["input_data"] == {"prompt": q, "docs": "# docs", "app": "AbstractGateway", "use_session_history": True}
    # One conversation = one gateway session (the server replays the first turn).
    assert first["session_id"] == second["session_id"]
    assert first["session_id"].startswith("gateway-docs-assistant:")
    assert out["replay"] == [{"text": "", "hidden": True}, {"text": "", "hidden": True}]


def test_the_receipt_is_shown_when_earlier_messages_were_not_replayed() -> None:
    receipt = {"replayed_messages": 12, "dropped_messages": 49, "dropped_tokens": 61234, "max_tokens": 50000}
    out = _drive({"questions": ["q?"], "receipts": [receipt]})
    assert out["replay"][0]["hidden"] is False
    assert out["replay"][0]["text"] == (
        "Earlier messages not replayed: 49 (~61,234 tokens). The model read the newest 12 messages "
        "(history window: the most recent 50,000 tokens of whole messages)."
    )


def test_new_conversation_starts_a_new_session_and_hides_the_receipt() -> None:
    receipt = {"replayed_messages": 12, "dropped_messages": 3, "max_tokens": 50000}
    out = _drive({"questions": ["a?", "__clear__", "b?"], "receipts": [receipt, None]})
    a, b = out["starts"]
    assert a["session_id"] != b["session_id"]
    assert out["replay"][1] == {"text": "", "hidden": True}
