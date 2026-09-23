"""Console first-run wizard (2026-09-23).

Pins the DOM contract later workstreams embed into, and drives the real
console JavaScript in a node VM: a `#claim=` link is stripped from the URL
BEFORE it is redeemed, redemption opens the wizard, every step renders from
the gateway's payloads (including the graceful "engine detection arrives with
the next AbstractCore release" card when `/engines` 404s), and Finish records
the first-run state with the CSRF header.
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

WIZARD_IDS = [
    "open-setup",
    "first-run-backdrop",
    "first-run-wizard",
    "first-run-title",
    "first-run-steps",
    "first-run-step-welcome",
    "first-run-step-engines",
    "first-run-step-model",
    "first-run-step-apps",
    "first-run-step-done",
    "first-run-host-summary",
    "first-run-engines-body",
    "first-run-model-body",
    "first-run-apps-body",
    "first-run-done-body",
    "first-run-message",
    "first-run-back",
    "first-run-next",
    "first-run-skip",
    "first-run-finish",
]


def test_wizard_dom_contract_is_present() -> None:
    html = gateway_console_html()
    for element_id in WIZARD_IDS:
        assert html.count(f'id="{element_id}"') == 1, element_id
    for fn in ("redeemClaimFromHash", "openFirstRunWizard", "maybeOpenFirstRun", "firstRunGoto"):
        assert f"function {fn}(" in html
    assert "/api/gateway/session/claim" in html
    assert "/api/gateway/host/first-run" in html
    for pkg in ("flow", "code", "observer", "continuum", "entity"):
        assert f"@abstractframework/{pkg}" in html


_HARNESS = r"""
import vm from "node:vm";
const source = __SOURCE__;
const scenario = __SCENARIO__;

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
}
const elements = new Map();
const initialClasses = __CLASSES__;
const el = (id) => {
  if (!elements.has(id)) { const e = new Element(id); e.className = initialClasses[id] || ""; elements.set(id, e); }
  return elements.get(id);
};
const document = { body: el("body"), documentElement: el("html"), cookie: "", getElementById: el, createElement: (t) => new Element(t) };
const store = new Map();
const localStorage = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)) };
const location = { hash: scenario.hash, pathname: "/console", search: "", origin: "http://127.0.0.1:18080", reload() {} };
const history = { calls: [], replaceState(_s, _t, url) { this.calls.push({ url, hashAtCall: location.hash, claimCallsSoFar: calls.filter((c) => c.path.endsWith("/session/claim")).length }); location.hash = ""; } };
let loggedIn = false;
let firstRunCompleted = scenario.completed;
const calls = [];
const res = (status, payload) => ({ ok: status >= 200 && status < 300, status, text: async () => JSON.stringify(payload) });
async function fetch(path, options = {}) {
  const method = options.method || "GET";
  const headers = options.headers || {};
  const csrf = typeof headers.get === "function" ? headers.get("X-AbstractGateway-CSRF") : null;
  calls.push({ path, method, body: options.body || "", csrf });
  if (path === "/api/gateway/session/claim" && method === "POST") {
    const body = JSON.parse(String(options.body || "{}"));
    if (body.code !== scenario.code) return res(401, { detail: { reason_code: "claim_unknown", message: "already used" } });
    loggedIn = true; document.cookie = "abstractgateway_csrf=agcsrf_wizard";
    return res(200, { ok: true, claimed: true, principal: { admin: true } });
  }
  if (path === "/api/gateway/me") {
    if (!loggedIn) return res(401, { detail: "signed out" });
    return res(200, { ok: true, principal: { tenant_id: "default", user_id: "admin", runtime_id: "default", roles: ["admin", "user"], admin: true } });
  }
  if (path === "/api/gateway/host/first-run" && method === "GET") return res(200, { ok: true, completed: firstRunCompleted });
  if (path === "/api/gateway/host/first-run" && method === "POST") { firstRunCompleted = true; return res(200, { ok: true, completed: true }); }
  if (path === "/api/gateway/host/state") return res(200, {
    ok: true,
    memory: { ram: { total_bytes: 34359738368 } },
    gpu: { supported: true, gpus: [{ name: "Apple M5 Max" }] },
    gateway: { data_dir: "/Users/u/Library/Application Support/AbstractGateway", data_dir_source: "os_default", auth_mode: "users", service: { installed: false, mechanism: "launchd-agent" }, url: "http://127.0.0.1:18080" },
  });
  if (path === "/api/gateway/engines") return res(404, { detail: "Not Found" });
  if (path === "/api/gateway/models/availability") return res(200, {
    routes: [],
    recommended: { total: 1, recommended: [{ route: "output.text", provider: "lmstudio", artifact: "qwen/qwen3.5-9b@4bit", status: "absent" }], gaps: [] },
  });
  if (path === "/api/gateway/config/capability-defaults") return res(200, { routes: [] });
  if (path === "/api/gateway/admin/users") return res(200, { users: [] });
  if (path === "/api/gateway/admin/runtime-reservations") return res(200, { runtime_reservations: [] });
  if (path === "/api/gateway/config/provider-endpoint-profiles") return res(200, { profiles: [] });
  if (path === "/api/gateway/discovery/providers") return res(200, { items: [] });
  return res(200, {});
}
const context = vm.createContext({
  document, fetch, Headers, localStorage, location, history, console, Blob,
  URL: { createObjectURL: () => "blob:x", revokeObjectURL() {} },
  Intl, navigator: { languages: ["en-US"], language: "en-US" },
  setTimeout: (fn, ms, ...a) => { const t = setTimeout(fn, Math.min(ms || 0, 5), ...a); t.unref(); return t; },
  clearTimeout, setInterval: () => 0, clearInterval() {}, encodeURIComponent, decodeURIComponent,
});
const settle = async () => { for (let i = 0; i < 60; i++) await new Promise((r) => setTimeout(r, 1)); };
const fail = (msg) => { throw new Error(msg); };
const hidden = (id) => String(el(id).className).split(/\s+/).includes("hidden");

vm.runInContext(source, context);
await settle();

if (scenario.name === "claim") {
  if (!history.calls.length) fail("claim code was not stripped from the URL");
  if (history.calls[0].claimCallsSoFar !== 0) fail("the code was sent before it was stripped from the address bar");
  if (String(history.calls[0].url).includes("claim")) fail("replaceState kept the claim: " + history.calls[0].url);
  const claim = calls.find((c) => c.path === "/api/gateway/session/claim");
  if (!claim || JSON.parse(claim.body).code !== scenario.code) fail("claim was not redeemed with the code");
  if (!String(document.body.className).includes("signed-in")) fail("claim did not produce a signed-in console");
  if (hidden("first-run-backdrop")) fail("wizard did not open after a claim");
  if (hidden("open-setup")) fail("Setup button hidden for an admin");
  const welcome = el("first-run-host-summary").innerHTML;
  for (const want of ["Apple M5 Max", "os_default", "Application Support", "32.0 GiB"]) if (!welcome.includes(want)) fail("welcome missing " + want + ": " + welcome);

  context.firstRunStep(1); await settle();
  const engines = el("first-run-engines-body").innerHTML;
  for (const want of ["Engine detection arrives with the next AbstractCore release", "https://ollama.com/download", "https://lmstudio.ai/download"]) if (!engines.includes(want)) fail("engines missing " + want);
  if (hidden("first-run-step-engines") || !hidden("first-run-step-welcome")) fail("step panels did not switch");

  context.firstRunStep(1); await settle();
  const model = el("first-run-model-body").innerHTML;
  for (const want of ["qwen/qwen3.5-9b@4bit", "not downloaded", "Use recommended defaults", "first-run-download"]) if (!model.includes(want)) fail("model step missing " + want + ": " + model);

  context.firstRunStep(1); await settle();
  const apps = el("first-run-apps-body").innerHTML;
  for (const pkg of ["flow", "code", "observer", "continuum", "entity"]) if (!apps.includes("npx @abstractframework/" + pkg)) fail("apps missing " + pkg);
  if (!apps.includes("http://127.0.0.1:18080")) fail("apps step did not name the gateway URL");

  context.firstRunStep(1); await settle();
  if (!el("first-run-done-body").innerHTML.includes("abstractgateway claim --open")) fail("done step missing the CLI equivalents");
  if (hidden("first-run-finish") || !hidden("first-run-next")) fail("finish/next buttons wrong on the last step");

  el("first-run-finish").onclick(); await settle();
  const post = calls.find((c) => c.path === "/api/gateway/host/first-run" && c.method === "POST");
  if (!post) fail("finish did not record the first-run state");
  if (JSON.parse(post.body).outcome !== "finished") fail("finish outcome wrong: " + post.body);
  if (post.csrf !== "agcsrf_wizard") fail("finish POST carried no CSRF header");
  if (!hidden("first-run-backdrop")) fail("wizard stayed open after finish");
}

if (scenario.name === "completed") {
  loggedIn = true;
  vm.runInContext("refresh()", context);
  await settle();
  if (!String(document.body.className).includes("signed-in")) fail("session did not render");
  if (!hidden("first-run-backdrop")) fail("wizard auto-opened on a completed data dir");
  if (hidden("open-setup")) fail("Setup button must stay reachable after completion");
  el("open-setup").onclick(); await settle();
  if (hidden("first-run-backdrop")) fail("Setup button did not reopen the wizard");
}
console.log("OK");
"""


def _run(scenario: dict) -> subprocess.CompletedProcess:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the console wizard smoke")
    html = gateway_console_html()
    scripts = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    # Stub elements start with the classes the REAL markup gives them (the
    # wizard backdrop and the Setup button start `hidden`).
    classes = {}
    for element_id in WIZARD_IDS:
        tag = re.search(r'<[^>]*\bid="%s"[^>]*>' % re.escape(element_id), html)
        cls = re.search(r'\bclass="([^"]*)"', tag.group(0)) if tag else None
        classes[element_id] = cls.group(1) if cls else ""
    harness = (
        _HARNESS.replace("__SOURCE__", json.dumps("\n".join(scripts)))
        .replace("__SCENARIO__", json.dumps(scenario))
        .replace("__CLASSES__", json.dumps(classes))
    )
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", encoding="utf-8", delete=False) as f:
        f.write(harness)
        path = f.name
    return subprocess.run([node, path], capture_output=True, text=True, check=False, timeout=60)


def test_claim_link_signs_in_opens_and_completes_the_wizard() -> None:
    code = "agclaim_" + "A" * 43
    result = _run({"name": "claim", "hash": f"#claim={code}", "code": code, "completed": False})
    assert result.returncode == 0, result.stderr + result.stdout
    assert "OK" in result.stdout


def test_completed_data_dir_does_not_auto_open_but_setup_reopens() -> None:
    result = _run({"name": "completed", "hash": "", "code": "", "completed": True})
    assert result.returncode == 0, result.stderr + result.stdout
    assert "OK" in result.stdout
