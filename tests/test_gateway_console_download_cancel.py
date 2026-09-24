"""Mission KK (2026-09-24): the console never ends a download on its own, and
says why a download ended.

On a fresh 24 GiB Mac the guide's "Chat and text" tile ended a running MTP
companion download as "Cancelled" -- "Download cancelled. Download it again
any time." -- with nobody meaning to cancel it. Reproduced in a real browser
(untracked/missionKK/ui_repro.mjs): the card re-renders every <= 0.5 s, which
closed the "Files (8 of 9 done)" list each time, and the "Cancel download"
button right below it jumped into the place the file rows had been; the next
click on the list was a cancel, posted at once.

Pinned here, by driving the REAL console JavaScript in a node VM:
  - re-rendering a running download (hundreds of paints) never posts a cancel;
  - one click on Cancel only ASKS ("Stop this download?", "Keep downloading"
    in the Cancel button's own spot); a double click is not an answer; only
    "Stop download" >= 0.4 s later posts, with `{"via": "console"}`;
  - a <details> keeps its open state across paints (the file list stays put);
  - a failed job shows its plain `ended_reason`, a cancelled one says who.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile

import pytest

from abstractgateway.console import gateway_console_html

pytestmark = pytest.mark.basic

_HARNESS = r"""
import vm from "node:vm";
const source = __SOURCE__;
const realSetTimeout = setTimeout;
class Element {
  constructor(id) {
    this.id = id; this.value = ""; this.checked = false; this.disabled = false;
    this._textContent = ""; this.innerHTML = ""; this.className = ""; this.style = {}; this.children = []; this.dataset = {};
    this.classList = {
      add: (...n) => { const s = new Set(String(this.className).split(/\s+/).filter(Boolean)); n.forEach((x) => s.add(x)); this.className = [...s].join(" "); },
      remove: (...n) => { const r = new Set(n); this.className = String(this.className).split(/\s+/).filter((x) => x && !r.has(x)).join(" "); },
      toggle: (name, force) => { const s = new Set(String(this.className).split(/\s+/).filter(Boolean)); const add = force === undefined ? !s.has(name) : Boolean(force); if (add) s.add(name); else s.delete(name); this.className = [...s].join(" "); },
      contains: (name) => new Set(String(this.className).split(/\s+/).filter(Boolean)).has(name),
    };
  }
  get textContent() { return this._textContent; }
  set textContent(v) { this._textContent = String(v || ""); if (!this._textContent) this.children = []; }
  get options() { return this.children; }
  get selectedOptions() { return this.children.filter((c) => c.selected); }
  append(...items) { this.children.push(...items); }
  addEventListener() {} setAttribute() {} focus() {}
}
const elements = new Map();
const el = (id) => { if (!elements.has(id)) elements.set(id, new Element(id)); return elements.get(id); };
const docListeners = {};
const document = { body: el("body"), documentElement: el("html"), cookie: "abstractgateway_csrf=x", getElementById: el, createElement: (t) => new Element(t),
  addEventListener: (type, fn) => { (docListeners[type] = docListeners[type] || []).push(fn); } };
const calls = [];
let jobState = { job_id: "dl_kk", job: "dl_kk", provider: "mlx", artifact: "org/Model-4bit", status: "running", state: "downloading", bytes_done: 100, bytes_total: 266,
  files: [{ name: "org/Model-MTP-4bit/model.safetensors", bytes_done: 50, bytes_total: 200, state: "downloading" }, { name: "org/Model-MTP-4bit/config.json", bytes_done: 2, bytes_total: 2, state: "done" }] };
const res = (status, payload) => ({ ok: status >= 200 && status < 300, status, text: async () => JSON.stringify(payload) });
async function fetch(path, options = {}) {
  const method = options.method || "GET";
  calls.push({ path, method, body: options.body || "" });
  if (path === "/api/gateway/me") return res(401, { detail: "signed out" });
  if (/\/models\/download\/[^/]+\/cancel$/.test(path)) { jobState = Object.assign({}, jobState, { cancel_requested: true, cancelled_by: "console" }); return res(200, { ok: true, job: jobState }); }
  if (path.startsWith("/api/gateway/models/download/")) return res(200, { ok: true, job: jobState });
  return res(200, {});
}
const context = vm.createContext({ window: {}, document, fetch, Headers, localStorage: { getItem: () => null, setItem() {} },
  location: { hash: "", pathname: "/console", search: "", origin: "http://127.0.0.1:1", reload() {} }, history: { replaceState() {} }, console, Blob,
  URL: { createObjectURL: () => "blob:x", revokeObjectURL() {} }, Intl, navigator: { languages: ["en"], language: "en" },
  setTimeout: (fn, ms, ...a) => { const t = realSetTimeout(fn, Math.min(ms || 0, 5), ...a); t.unref(); return t; }, clearTimeout, setInterval: () => 0, clearInterval() {},
  encodeURIComponent, decodeURIComponent });
const settle = async () => { for (let i = 0; i < 40; i++) await new Promise((r) => realSetTimeout(r, 1)); };
const sleep = (ms) => new Promise((r) => realSetTimeout(r, ms));
const fail = (m) => { throw new Error(m); };
const run = (code) => vm.runInContext(code, context);
run(source);
await settle();
const cancels = () => calls.filter((c) => /\/cancel$/.test(c.path));
run(`firstRun.open = true; firstRun.step = "model";
  state.availabilityPlan = { recommended: [{ route: "input.text", provider: "mlx", artifact: "org/Model-4bit", status: "absent", warning: "w" }] };`);
const tile = () => el("first-run-model-recommended").innerHTML;
run(`dlApply(${JSON.stringify(jobState)}, []); dlFeed.ids.add("dl_kk");`);

// 1. Hundreds of paints of a running download never post a cancel.
for (let i = 0; i < 300; i++) run(`dlApply(Object.assign({}, state.downloadJobs.get(downloadJobKey("mlx", "org/Model-4bit")), { bytes_done: ${100 + i} }), []); dlRender();`);
await settle();
if (cancels().length) fail("a re-render posted a cancel: " + JSON.stringify(cancels()));
if (!tile().includes('data-dl-cancel="dl_kk"') || !tile().includes("Cancel download")) fail("no Cancel download on a running tile: " + tile());
if (/\/cancel/.test(run("renderFirstRunModel.toString() + dlRender.toString() + dlApply.toString() + dlIngest.toString() + dlPoll.toString()"))) fail("a render/feed function names the cancel route");

// 2. The file list keeps its open state across paints.
if (!tile().includes('data-ui-open-key="files:dl_kk"')) fail("the files <details> has no key: " + tile());
for (const fn of docListeners.toggle || []) fn({ target: { open: true, dataset: { uiOpenKey: "files:dl_kk" } } });
run("dlRender()");
if (!/data-ui-open-key="files:dl_kk" open/.test(tile())) fail("an opened file list closed on the next paint: " + tile());

// 3. One click asks; a double click is not an answer; Keep downloading keeps it.
await run(`dlCancel("dl_kk", null)`); await settle();
if (cancels().length) fail("ONE click on Cancel posted a cancel");
const asked = tile();
if (!asked.includes("Stop this download?") || !asked.includes('data-dl-step="keep"') || !asked.includes('data-dl-step="confirm"')) fail("the first click did not ask: " + asked);
if (asked.indexOf('data-dl-step="keep"') > asked.indexOf('data-dl-step="confirm"')) fail("Keep downloading must sit first, in the Cancel button's spot");
await run(`dlCancel("dl_kk", null, "confirm")`); await settle();
if (cancels().length) fail("a confirm within 0.4 s (a double click) posted a cancel");
await run(`dlCancel("dl_kk", null, "keep")`); await settle();
if (tile().includes("Stop this download?")) fail("Keep downloading did not close the question");
await run(`dlCancel("dl_kk", null, "confirm")`); await settle();
if (cancels().length) fail("a confirm without a question posted a cancel");

// 4. Ask, wait, Stop download: ONE post, saying it came from a person in the console.
await run(`dlCancel("dl_kk", null)`); await sleep(450);
await run(`dlCancel("dl_kk", null, "confirm")`); await settle();
if (cancels().length !== 1) fail("Stop download must post exactly once: " + JSON.stringify(cancels()));
if (JSON.parse(cancels()[0].body).via !== "console") fail("the cancel must say via: console: " + cancels()[0].body);

// 5. Final states say why.
const failed = Object.assign({}, jobState, { status: "failed", state: "failed", message: "raw", error: "httpx.RemoteProtocolError: peer closed", ended_reason: "The connection to Hugging Face dropped after 200 MB of 266 MB. Check the network connection, then download it again." });
run(`state.downloadJobs.set(downloadJobKey("mlx", "org/Model-4bit"), ${JSON.stringify(failed)}); dlRender();`);
if (!tile().includes("The connection to Hugging Face dropped after 200 MB of 266 MB.") || tile().includes("Cancelled")) fail("a failed job must show its plain reason, never Cancelled: " + tile());
if (!tile().includes("httpx.RemoteProtocolError")) fail("the verbatim error belongs behind Show details: " + tile());
const cancelled = Object.assign({}, jobState, { status: "cancelled", state: "cancelled", ended_reason: "Cancelled in the console by admin at 21:37 after 63 MB of 275 MB." });
run(`state.downloadJobs.set(downloadJobKey("mlx", "org/Model-4bit"), ${JSON.stringify(cancelled)}); dlRender();`);
if (!tile().includes("Cancelled in the console by admin at 21:37")) fail("a cancelled job must say who cancelled it: " + tile());
console.log("OK");
"""


def _run() -> subprocess.CompletedProcess:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the console download smoke")
    scripts = re.findall(r"<script>(.*?)</script>", gateway_console_html(), flags=re.S)
    harness = _HARNESS.replace("__SOURCE__", json.dumps("\n".join(scripts)))
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", encoding="utf-8", delete=False) as f:
        f.write(harness)
        path = f.name
    return subprocess.run([node, path], capture_output=True, text=True, check=False, timeout=60)


def test_the_console_never_cancels_on_its_own_and_asks_before_it_cancels() -> None:
    proc = _run()
    assert proc.returncode == 0 and proc.stdout.strip().endswith("OK"), proc.stdout[-3000:] + proc.stderr[-3000:]


def test_every_cancel_control_is_wired_with_its_step() -> None:
    html = gateway_console_html()
    # The only request to the cancel route is inside dlCancel's confirm branch.
    assert len(re.findall(r"/models/download/\$\{encodeURIComponent\(jobId\)\}/cancel", html)) == 1
    # Every click handler passes the step (ask / keep / confirm) through.
    handlers = re.findall(r"dlCancel\([^)]*\)", html)
    wired = [h for h in handlers if "dataset" in h]
    assert wired and all("dlStep" in h for h in wired), wired
