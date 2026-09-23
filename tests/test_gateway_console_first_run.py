"""Console first-run wizard (2026-09-23).

Pins the DOM contract later workstreams embed into, and drives the real
console JavaScript in a node VM: a `#claim=` link is stripped from the URL
BEFORE it is redeemed, redemption opens the wizard, every step renders from
the gateway's payloads, and Finish records the first-run state with the CSRF
header. The Engines and Model steps mount AbstractCore's embedded screens
(`window.AbstractCoreConsole`, stubbed here) with the gateway's options; the
"Set as default" bar writes the text route through the capability-defaults
endpoint; an AbstractCore older than 2.14.0 shows a card instead.
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
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  dispatch(type, event) { for (const fn of (this.listeners[type] || [])) fn(event); }
  get listeners() { if (!this._listeners) this._listeners = {}; return this._listeners; }
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
  if (path === "/api/gateway/models/delete") return res(409, { ok: false, status: "refused", message: "refusing to delete: loaded (send force=true to override)", delete_blockers: ["loaded"], error: { message: "refusing to delete: loaded (send force=true to override)", type: "host_action_refused" } });
  if (path.startsWith("/api/gateway/config/capability-defaults/") && method === "PUT") return res(200, { ok: true });
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
// AbstractCore's embedded screens, stubbed: records every mount/refresh/unmount.
const mounts = [];
const coreLib = {
  mount(kind, rootEl, options) {
    const rec = { kind, id: rootEl.id, options, refreshed: 0, unmounted: 0 };
    mounts.push(rec);
    return { kind, refresh() { rec.refreshed += 1; }, unmount() { rec.unmounted += 1; } };
  },
};
const windowStub = scenario.coreStub ? { AbstractCoreConsole: coreLib } : {};
const context = vm.createContext({
  window: windowStub,
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
  if (hidden("first-run-step-engines") || !hidden("first-run-step-welcome")) fail("step panels did not switch");
  if (scenario.expectMount) {
    const m = mounts.find((x) => x.id === "first-run-engines-body");
    if (!m || m.kind !== "engines") fail("engines step did not mount the Engines screen: " + JSON.stringify(mounts.map((x) => [x.kind, x.id])));
    const o = m.options;
    if (o.apiBase !== "/api/gateway" || o.cliPrefix !== "abstractgateway") fail("mount options wrong: " + JSON.stringify(o));
    if (!o.hostName || typeof o.hostName !== "string") fail("mount carried no host name");
    if (o.isAdmin() !== true) fail("isAdmin must read the admin principal");
    if (typeof o.onJob !== "function") fail("mount carried no onJob");
    // The request adapter: CSRF on writes, `.status` and the refusal's own
    // message on a 409 whose body has no `detail` envelope.
    let refused = null;
    try { await o.request("POST", "/api/gateway/models/delete", { provider: "ollama", artifact: "qwen3:8b" }); } catch (err) { refused = err; }
    if (!refused || refused.status !== 409) fail("adapter must reject with .status 409");
    if (!String(refused.message).startsWith("refusing to delete: loaded")) fail("adapter lost the refusal message: " + refused.message);
    const del = calls.find((c) => c.path === "/api/gateway/models/delete");
    if (!del || del.csrf !== "agcsrf_wizard" || JSON.parse(del.body).artifact !== "qwen3:8b") fail("adapter POST carried no CSRF/body: " + JSON.stringify(del));
    const got = await o.request("GET", "/api/gateway/host/first-run");
    if (!got || got.ok !== true) fail("adapter GET did not resolve to parsed JSON");
    // Going back and forth refreshes the mounted screen, never re-mounts it.
    context.firstRunStep(-1); await settle(); context.firstRunStep(1); await settle();
    if (mounts.filter((x) => x.id === "first-run-engines-body").length !== 1 || m.refreshed < 1) fail("engines screen re-mounted instead of refreshed");
  } else {
    const engines = el("first-run-engines-body").innerHTML;
    for (const want of scenario.enginesWant) if (!engines.includes(want)) fail("engines fallback missing " + want + ": " + engines);
  }

  context.firstRunStep(1); await settle();
  const model = el("first-run-model-recommended").innerHTML;
  for (const want of ["qwen/qwen3.5-9b@4bit", "not downloaded", "Use recommended defaults", "first-run-download"]) if (!model.includes(want)) fail("model step missing " + want + ": " + model);
  if (scenario.expectMount) {
    const m = mounts.find((x) => x.id === "first-run-model-catalog");
    if (!m || m.kind !== "models" || m.options.apiBase !== "/api/gateway") fail("model step did not mount the Models screen");
    // Selecting an installed row offers "Set as default"; it PUTs the text
    // route with the SERVED id (LM Studio's @quant suffix dropped) + CSRF.
    const row = { dataset: { provider: "lmstudio", artifact: "qwen/qwen3.5-9b@4bit" } };
    el("first-run-model-catalog").dispatch("click", { target: { closest: (sel) => (sel.includes('data-acc-row="installed"') ? row : null) } });
    if (!el("first-run-model-default").innerHTML.includes("first-run-set-default")) fail("installed row did not offer Set as default: " + el("first-run-model-default").innerHTML);
    el("first-run-model-default").innerHTML = "";
    el("first-run-model-catalog").dispatch("click", { target: { closest: () => null } });
    if (el("first-run-model-default").innerHTML.includes("first-run-set-default")) fail("a non-installed click must not offer Set as default");
    el("first-run-model-catalog").dispatch("focusin", { target: { closest: (sel) => (sel.includes('data-acc-row="installed"') ? row : null) } });
    await el("first-run-set-default").onclick(); await settle();
    const put = calls.find((c) => c.method === "PUT" && c.path.startsWith("/api/gateway/config/capability-defaults/"));
    if (!put || put.path !== "/api/gateway/config/capability-defaults/output/text") fail("Set as default hit the wrong route: " + JSON.stringify(put));
    const body = JSON.parse(put.body);
    if (body.provider !== "lmstudio" || body.model !== "qwen/qwen3.5-9b") fail("Set as default body wrong: " + put.body);
    if (put.csrf !== "agcsrf_wizard") fail("Set as default PUT carried no CSRF header");
    if (!el("first-run-message").textContent.includes("qwen/qwen3.5-9b")) fail("no confirmation after Set as default");
  } else {
    const cat = el("first-run-model-catalog").innerHTML;
    if (!cat.includes(scenario.enginesWant[0])) fail("model step must explain why the catalog is missing: " + cat);
  }

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
  if (scenario.expectMount) {
    for (const id of ["first-run-engines-body", "first-run-model-catalog"]) {
      const m = mounts.find((x) => x.id === id);
      if (!m || m.unmounted !== 1) fail("closing the wizard must unmount " + id);
    }
    // The Models / Engines tabs mount on first open, refresh on re-open.
    el("tab-button-catalog").onclick(); await settle();
    el("tab-button-engines").onclick(); await settle();
    el("tab-button-catalog").onclick(); await settle();
    const tabModels = mounts.filter((x) => x.id === "catalog-core-root");
    const tabEngines = mounts.filter((x) => x.id === "engines-core-root");
    if (tabModels.length !== 1 || tabModels[0].kind !== "models") fail("Models tab did not mount once: " + JSON.stringify(mounts.map((x) => [x.kind, x.id])));
    if (tabEngines.length !== 1 || tabEngines[0].kind !== "engines") fail("Engines tab did not mount once");
    if (tabModels[0].refreshed !== 1) fail("re-opening the Models tab must refresh it");
    if (tabModels[0].options.cliPrefix !== "abstractgateway" || tabModels[0].options.apiBase !== "/api/gateway") fail("tab mount options wrong");
    if (!String(el("tab-catalog").className).includes("active")) fail("Models tab panel not active");
  } else {
    el("tab-button-catalog").onclick(); await settle();
    el("tab-button-engines").onclick(); await settle();
    if (mounts.length) fail("nothing may mount without the AbstractCore screens");
  }
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


def _run(scenario: dict, html: str | None = None) -> subprocess.CompletedProcess:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the console wizard smoke")
    html = html if html is not None else gateway_console_html()
    scripts = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    # Stub elements start with the classes the REAL markup gives them (the
    # wizard backdrop and the Setup button start `hidden`).
    classes = {}
    for element_id in WIZARD_IDS + ["tab-catalog", "tab-engines"]:
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
    result = _run({"name": "claim", "hash": f"#claim={code}", "code": code, "completed": False, "coreStub": True, "expectMount": True})
    assert result.returncode == 0, result.stderr + result.stdout
    assert "OK" in result.stdout


def test_wizard_without_the_screens_script_explains_and_keeps_download_links() -> None:
    # Server has the screens, but the page's AbstractCore script did not run.
    code = "agclaim_" + "B" * 43
    result = _run({
        "name": "claim", "hash": f"#claim={code}", "code": code, "completed": False, "coreStub": False,
        "enginesWant": ["did not load", "https://ollama.com/download", "https://lmstudio.ai/download", "first-run-engines-fallback"],
    })
    assert result.returncode == 0, result.stderr + result.stdout
    assert "OK" in result.stdout


def test_wizard_with_an_older_abstractcore_shows_the_upgrade_card(monkeypatch) -> None:
    from abstractgateway import core_config

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
    code = "agclaim_" + "C" * 43
    result = _run({
        "name": "claim", "hash": f"#claim={code}", "code": code, "completed": False, "coreStub": True,
        "enginesWant": ["require abstractcore \u2265 2.14.0", "2.13.42", 'pip install -U &quot;abstractcore&gt;=2.14.0&quot;', "https://ollama.com/download"],
    }, html=html)
    # A global that happens to exist is ignored when the server said the
    # screens are unavailable: the page never mounts (expectMount False).
    assert result.returncode == 0, result.stderr + result.stdout


def test_completed_data_dir_does_not_auto_open_but_setup_reopens() -> None:
    result = _run({"name": "completed", "hash": "", "code": "", "completed": True})
    assert result.returncode == 0, result.stderr + result.stdout
    assert "OK" in result.stdout
