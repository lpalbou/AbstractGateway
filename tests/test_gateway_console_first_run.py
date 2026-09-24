"""Console first-run wizard (2026-09-23).

Pins the DOM contract later workstreams embed into, and drives the real
console JavaScript in a node VM: a `#claim=` link is stripped from the URL
BEFORE it is redeemed, redemption opens the wizard, every step renders from
the gateway's payloads, and Finish records the first-run state with the CSRF
header. The Engines and Model steps mount AbstractCore's embedded screens
(`window.AbstractCoreConsole`, stubbed here) with the gateway's options; an
AbstractCore older than 2.14.0 shows a card instead.

Mission L (2026-09-24): the guide is a full-page flow; Local engines render
as CONSOLE cards from GET /engines (one card per engine, one primary action
per state, plain-language failures, the log behind "Show details") instead
of AbstractCore's table; the Apps step shows Install/Open cards with the
terminal commands behind "Technical details".

Mission X2 (2026-09-24): the Model step shows the Models tab's catalog CARDS
(console_catalog.py) with "Fits this computer" preset, not AbstractCore's
table; a downloaded text model's "Use as default" writes the text route
through the capability-defaults endpoint; the Models tab mounts the cards
plus AbstractCore's screen (its "on this computer" list) below them.
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
    "first-run-scroll",
    "first-run-kicker",
    "first-run-step-title",
    "first-run-step-lede",
    "first-run-advanced",
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
const history = { calls: [], replaceState(_s, _t, url) { this.calls.push({ url, hashAtCall: location.hash, claimCallsSoFar: calls.filter((c) => c.path.endsWith("/session/claim")).length }); location.hash = (String(url).match(/#.*$/) || [""])[0]; } };
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
    return res(200, Object.assign({ ok: true, claimed: true, principal: { admin: true } }, scenario.createdBy ? { claim: { created_by: scenario.createdBy } } : {}));
  }
  if (path === "/api/gateway/me") {
    if (!loggedIn) return res(401, { detail: "signed out" });
    return res(200, Object.assign({ ok: true, principal: { tenant_id: "default", user_id: "admin", runtime_id: "default", roles: ["admin", "user"], admin: true } }, scenario.meAuth ? { auth: scenario.meAuth } : {}));
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
  if (path.startsWith("/api/gateway/engines?")) {
    if (scenario.enginesFail) return res(501, { detail: { message: scenario.enginesFail } });
    return res(200, { schema: "gateway_engines_v2", install_allowed: true, engines: [
      { id: "ollama", name: "Ollama", description: "A local model server.", supported: true, installed: false, running: false, base_url: "http://localhost:11434", install: { available: true, method: "app", needs_admin: false, steps: ["download"], command_preview: ["download ollama"] }, actions: [{ id: "install", label: "Install", enabled: true }, { id: "docs", label: "Docs", enabled: true, url: "https://docs.ollama.com" }], active_job: null },
      { id: "mlx", name: "MLX (mlx-lm)", description: "Apple's engine.", supported: true, installed: true, version: "0.32.2", running: null, install: { available: true, method: "wheel" }, actions: [], active_job: null },
    ] });
  }
  if (path === "/api/gateway/engines/jobs") return res(200, { jobs: [] });
  if (path === "/api/gateway/engines/ollama/install" && method === "POST" && JSON.parse(String(options.body || "{}")).dry_run === true) {
    const loc = JSON.parse(String(options.body || "{}")).location;
    const sys = loc === "system";
    return res(200, { schema: "engine_install_job_v1", job_id: null, dry_run: true, state: "done", plan: { available: true, method: "app", target: sys ? "/Applications/Ollama.app" : "/Users/u/Applications/Ollama.app", needs_admin: sys, admin_reason: sys ? "/Applications is not writable by this account; placing Ollama.app there needs an administrator." : null } });
  }
  if (path === "/api/gateway/network") return res(200, { schema: "gateway_network_v1", writable: true, configured: { mode: "localhost", label: "Localhost only", port: 18080 }, effective: { mode: "localhost", label: "Localhost only", bind_host: "127.0.0.1", port: 18080 }, restart_required: false, restart: {}, auth: { ok_for_mode: true }, modes: [{ id: "localhost", label: "Localhost only", allowed: true }, { id: "lan", label: "Local network", allowed: true }, { id: "internet", label: "Internet", allowed: true }], addresses: [{ kind: "loopback", url: "http://127.0.0.1:18080", reachable: true }], copy_hint: "http://127.0.0.1:18080", warnings: [] });
  if (path === "/api/gateway/engines/ollama/install" && method === "POST") return res(200, { schema: "engine_install_job_v1", job_id: "eng-1", engine: "ollama", state: "downloading", status: "running", percent: 40, bytes_done: 40, bytes_total: 100, message: "Downloading Ollama", can_cancel: true });
  if (path === "/api/gateway/engines/jobs/eng-1") return res(200, { schema: "engine_install_job_v1", job_id: "eng-1", engine: "ollama", state: "needs_tools", status: "running", percent: 5, message: "Building from source needs the Apple command-line tools.", tools_prompt: { reason: "no compiler", action: { kind: "xcode_select_install", available: true, button: "Install tools" } }, continue_actions: ["install_tools", "recheck"], can_cancel: true, details: "xcrun: error: invalid active developer path" });
  if (path === "/api/gateway/engines/jobs/eng-1/continue" && method === "POST") return res(200, { schema: "engine_install_job_v1", job_id: "eng-1", engine: "ollama", state: "failed", status: "failed", message: "The build failed: no C compiler was found.", error: { code: "build_failed", message: "The build failed: no C compiler was found." }, details: "xcrun: error: invalid active developer path", can_cancel: false });
  if (path.startsWith("/api/gateway/jobs?")) return res(200, { schema: "host_jobs_v1", jobs: [] });
  if (path.startsWith("/api/gateway/apps?")) {
    const row = (id, pkg, extra) => Object.assign({ id, name: id, package: pkg, installed: false, running: false, status: "not_installed", needs_node_install: true, install_available: false, install_blocked_reason: "Installing software on the gateway host is turned off for this gateway.", actions: [], active_job: null }, extra || {});
    return res(200, { ok: true, gateway_url: "http://127.0.0.1:18080", install_allowed: true, registry: { reachable: true }, runtime: { node: { available: false, active_job: null } },
      console_tui: { kind: "tui", installed: false, install_available: false, install_method: "cargo", install_command: "cargo install abstractgateway-console", command: "abstractgateway-console --url http://127.0.0.1:18080" }, apps: [
      row("flow", "@abstractframework/flow", { install_available: true, install_blocked_reason: null, actions: ["install"] }),
      row("code", "@abstractframework/code", { installed: true, version: "0.4.2", running: true, status: "running", actions: ["open", "stop", "logs"], url: "http://127.0.0.1:3002/", interfaces: [{ kind: "web" }, { kind: "tui", installed: true, version: "0.5.0", launch_available: true, command: "abstractcode --gateway http://127.0.0.1:18080" }] }),
      row("observer", "@abstractframework/observer"),
      row("continuum", "@abstractframework/continuum"),
      row("entity", "@abstractframework/entity"),
    ] });
  }
  if (path === "/api/gateway/apps/flow/install" && method === "POST") return res(200, { ok: true, created: true, job: { id: "app-1", kind: "install", app_id: "flow", state: "running", percent: 12, bytes_done: 0, bytes_total: null, message: "Installing Node.js", steps: [] } });
  if (path === "/api/gateway/apps/jobs/app-1") return res(200, { ok: true, job: { id: "app-1", kind: "install", app_id: "flow", state: "succeeded", percent: 100, message: "Flow is running" } });
  if (path === "/api/gateway/models/catalog") {
    if (scenario.catalogFail) return res(501, { detail: { message: scenario.catalogFail } });
    return res(200, CATALOG);
  }
  if (path === "/api/gateway/models/delete") return res(409, { ok: false, status: "refused", message: "refusing to delete: loaded (send force=true to override)", delete_blockers: ["loaded"], error: { message: "refusing to delete: loaded (send force=true to override)", type: "host_action_refused" } });
  if (path.startsWith("/api/gateway/config/capability-defaults/") && method === "PUT") return res(200, { ok: true });
  if (path === "/api/gateway/models/availability") return res(200, {
    routes: [],
    recommended: { total: 1, recommended: [{ route: "output.text", provider: "lmstudio", artifact: "qwen/qwen3.5-9b@4bit", status: "absent" }], gaps: [] },
  });
  if (path === "/api/gateway/config/capability-defaults") return res(200, { routes: [] });
  if (path === "/api/gateway/admin/users" && method === "POST") return res(409, { detail: { reason_code: "user_accounts_off_admin_only", message: "This gateway runs with user accounts off, so a non-admin account could not sign in here." } });
  if (path === "/api/gateway/admin/users") return res(200, { users: [] });
  if (path === "/api/gateway/admin/runtime-reservations") return res(200, { runtime_reservations: [] });
  if (path === "/api/gateway/config/provider-endpoint-profiles") return res(200, { profiles: [] });
  if (path === "/api/gateway/discovery/providers") return res(200, { items: [] });
  return res(200, {});
}
// AbstractCore's model_catalog_v1 (with W1's quant_class): one text model
// (installed LM Studio 4-bit build recommended, an 8-bit MLX build, an Ollama
// build too large for this host) and one model with nothing that fits.
const art = (provider, artifact, quant_class, verdict, status, extra) => Object.assign({
  provider, artifact, engine: provider, quant: quant_class, quant_class, bits: quant_class === "8bit" ? 8.5 : 4.5,
  download_bytes: 5400000000, size_source: "catalog", presence: { status, location: null, evidence: null },
  fit: { verdict, notes: [] }, supported_on_host: true, downloadable: true, recommended: false,
}, extra || {});
const CATALOG = { schema: "model_catalog_v1", host_profile: { accelerator: "metal", gpu_name: "Apple M5 Max", ram_bytes: 34359738368, unified_memory: true, ceiling_bytes: 27000000000 }, rows: [
  { id: "qwen3.5-9b", display_name: "Qwen3.5 9B", vendor: "Qwen", params_total: 9000000000, license: "apache-2.0", starter: true, capabilities: { text: true, tools: "native", thinking: true },
    artifacts: [
      art("lmstudio", "qwen/qwen3.5-9b@4bit", "4bit", "fits", "installed", { recommended: true }),
      art("mlx", "mlx-community/Qwen3.5-9B-8bit", "8bit", "fits", "absent"),
      art("ollama", "qwen3.5:9b-fp16", "16bit", "too_large", "absent"),
    ] },
  { id: "big-model", display_name: "Big Model 400B", vendor: "Big", params_total: 400000000000, license: "mit", starter: false, capabilities: { text: true },
    artifacts: [art("mlx", "mlx-community/Big-400B-4bit", "4bit", "too_large", "absent", { recommended: true })] },
] };
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
  if (mounts.some((x) => x.kind === "engines")) fail("engines must render as console cards, not mount AbstractCore's table: " + JSON.stringify(mounts.map((x) => [x.kind, x.id])));
  const engines = el("first-run-engines-body").innerHTML;
  if (engines.includes("<table")) fail("engines must not render as a table");
  if (engines.includes("CLI equivalent")) fail("no CLI lines on the primary path");
  if (scenario.enginesFail) {
    for (const want of scenario.enginesWant) if (!engines.includes(want)) fail("engines failure view missing " + want + ": " + engines);
  } else {
    for (const want of ['data-engine-card="ollama"', 'data-engine-card="mlx"', "Not installed", "Ready", 'data-engine-action="install"']) if (!engines.includes(want)) fail("engine cards missing " + want + ": " + engines);
    // Install asks first, then POSTs a real (non-dry-run) job with CSRF.
    await context.engineAction("install", "ollama"); await settle(); await settle();
    const confirmHtml = el("first-run-engines-body").innerHTML;
    if (!confirmHtml.includes('data-engine-action="install-go"')) fail("Install must confirm before running");
    // An app engine offers the two real locations (mission L2): the gateway's
    // own dry-run plans for user and system, the system one marked
    // "(administrator)" because its plan says needs_admin.
    const plans = calls.filter((c) => c.path === "/api/gateway/engines/ollama/install" && JSON.parse(c.body).dry_run === true).map((c) => JSON.parse(c.body).location).sort();
    if (JSON.stringify(plans) !== JSON.stringify(["system", "user"])) fail("location plans not asked: " + JSON.stringify(plans));
    if (!/data-location="user"[^>]*>Install</.test(confirmHtml) || !/data-location="system"[^>]*>Install for all users \(administrator\)</.test(confirmHtml)) fail("location choice missing: " + confirmHtml);
    await context.engineAction("install-go", "ollama", { dataset: { location: "user" } }); await settle(); await settle();
    const post = calls.find((c) => c.path === "/api/gateway/engines/ollama/install" && JSON.parse(c.body).dry_run === false);
    if (!post || post.csrf !== "agcsrf_wizard" || JSON.parse(post.body).location !== "user") fail("install POST wrong: " + JSON.stringify(post));
    if (!calls.some((c) => c.path === "/api/gateway/engines/jobs/eng-1")) fail("the install job was not polled");
    const waiting = el("first-run-engines-body").innerHTML;
    if (!waiting.includes("Needs Apple tools") || !waiting.includes('data-engine-action="continue:install_tools"') || !waiting.includes("Install tools")) fail("needs_tools must offer Install tools: " + waiting);
    await context.engineAction("continue:install_tools", "ollama"); await settle(); await settle();
    const cont = calls.find((c) => c.path === "/api/gateway/engines/jobs/eng-1/continue");
    if (!cont || cont.csrf !== "agcsrf_wizard" || JSON.parse(cont.body).action !== "install_tools") fail("continue POST wrong: " + JSON.stringify(cont));
    const after = el("first-run-engines-body").innerHTML;
    if (!after.includes("no C compiler was found")) fail("a failed install shows the job's plain message first: " + after);
    if (!after.includes("Show details") || !after.includes("ui-log")) fail("the raw log must sit behind Show details: " + after);
    if (!after.includes("Try again")) fail("a failed install offers Try again");
  }

  context.firstRunStep(1); await settle();
  const model = el("first-run-model-recommended").innerHTML;
  for (const want of ["qwen/qwen3.5-9b@4bit", "Not downloaded", "Use recommended defaults", "first-run-download"]) if (!model.includes(want)) fail("model step missing " + want + ": " + model);
  // The Models tab's catalog cards, "Fits this computer" preset (never
  // AbstractCore's table, whatever the screens' state).
  if (mounts.some((x) => x.id === "first-run-model-catalog")) fail("the model step must show the catalog cards, not mount AbstractCore's table");
  const cat = el("first-run-model-catalog").innerHTML;
  if (scenario.catalogFail) {
    for (const want of ["The model catalog did not load", scenario.catalogWant]) if (!cat.includes(want)) fail("model step must explain why the catalog is missing (" + want + "): " + cat);
  } else {
    for (const want of ['data-mc-model="qwen3.5-9b"', "Recommended for this computer", "qwen/qwen3.5-9b@4bit", "mlx-community/Qwen3.5-9B-8bit", "8-bit", "Starter", "Use as default", 'data-mc-fits="1" checked', "1</b> of 2 models"]) if (!cat.includes(want)) fail("catalog cards missing " + want + ": " + cat);
    for (const gone of ['data-mc-model="big-model"', "qwen3.5:9b-fp16"]) if (cat.includes(gone)) fail("the guide presets Fits this computer; " + gone + " is too large and must be hidden: " + cat);
    if (cat.includes("<table")) fail("the catalog is cards, not a table");
    if (cat.includes("does not report quant_class")) fail("a catalog WITH quant_class must not show the missing-field notice");
    // "Use as default" on the downloaded text model PUTs the text route with
    // the SERVED id (LM Studio's @quant suffix dropped) + CSRF.
    const btn = { disabled: false, textContent: "Use as default", dataset: { mcAction: "default", provider: "lmstudio", artifact: "qwen/qwen3.5-9b@4bit" } };
    el("first-run-model-catalog").onclick({ target: { closest: (sel) => (sel === "[data-mc-action]" ? btn : null) } });
    await settle();
    const put = calls.find((c) => c.method === "PUT" && c.path.startsWith("/api/gateway/config/capability-defaults/"));
    if (!put || put.path !== "/api/gateway/config/capability-defaults/output/text") fail("Use as default hit the wrong route: " + JSON.stringify(put));
    const body = JSON.parse(put.body);
    if (body.provider !== "lmstudio" || body.model !== "qwen/qwen3.5-9b") fail("Use as default body wrong: " + put.body);
    if (put.csrf !== "agcsrf_wizard") fail("Use as default PUT carried no CSRF header");
    if (!el("first-run-message").textContent.includes("qwen/qwen3.5-9b")) fail("no confirmation after Use as default");
  }

  context.firstRunStep(1); await settle();
  const apps = el("first-run-apps-body").innerHTML;
  // Mission GG: a plain card is icon + name + pill / one line / ONE action
  // row (the primary action + the terminal one), pinned at the same level
  // (`.is-aligned` subgrid). Nothing technical is RENDERED with Technical
  // details off: no Stop, no Show log, no versions, no commands.
  if (!apps.includes('class="ui-card-grid is-aligned"')) fail("the apps grid must pin the action rows (is-aligned): " + apps);
  for (const gone of ['data-app-action="stop"', 'data-app-action="logs"', "Show log", "npx @abstractframework/", "Version 0.4.2", "Open it with the Open button", "Also runs in your terminal", "abstractcode --gateway"]) if (apps.includes(gone)) fail("the plain view must not render " + gone + ": " + apps);
  if (!apps.includes("http://127.0.0.1:18080")) fail("apps step did not name the gateway URL");
  if (apps.includes("nodejs.org")) fail("apps step must not send people to nodejs.org");
  for (const want of ['data-app-action="install"', "Install and open", 'data-app-action="open"', 'data-app-action="tui-open"', "Open in Terminal", "Node.js will be installed for you", "turned off for this gateway"]) if (!apps.includes(want)) fail("apps cards missing " + want + ": " + apps);
  if (!/<button[^>]*is-primary[^>]*disabled[^>]*>Install</.test(apps)) fail("a blocked install shows a disabled Install with its reason: " + apps);
  const cards = apps.split('<article class="ui-card ui-app-card').slice(1);
  if (cards.length !== 5) fail("five app cards expected: " + cards.length);
  for (const c of cards) {
    const at = ["ui-card__head", "ui-card__blurb is-oneline", "ui-card__body", "ui-card__actions", "ui-card__tech"].map((k) => c.indexOf('class="' + k));
    if (at.some((i) => i < 0) || at.some((i, k) => k && i < at[k - 1])) fail("a card must be head / one line / body / action row / technical, in that order: " + c);
    const row = c.slice(c.indexOf('class="ui-card__actions"'), c.indexOf('class="ui-card__tech"'));
    if ((row.match(/is-primary/g) || []).length !== 1) fail("one primary action per card: " + row);
  }
  const codeRow = cards.find((c) => c.includes('data-app-card="code"'));
  if (!/data-app-action="open"[\s\S]*data-app-action="tui-open"[\s\S]*class="ui-card__tech"/.test(codeRow)) fail("Open in Terminal sits next to Open in the one action row: " + codeRow);
  // Technical details ON renders the technical line (Stop · Show log ·
  // version) and the commands; OFF removes them from the DOM again.
  context.uiSetAdvanced(true); await settle();
  const tech = el("first-run-apps-body").innerHTML;
  for (const want of ['data-app-action="stop"', 'data-app-action="logs"', "Show log", "Version 0.4.2", "Terminal 0.5.0", "abstractcode --gateway http://127.0.0.1:18080", "ui-card__techline"]) if (!tech.includes(want)) fail("Technical details must render " + want + ": " + tech);
  for (const pkg of ["flow", "code", "observer", "continuum", "entity"]) if (!tech.includes("npx @abstractframework/" + pkg)) fail("Technical details must show the npx line for " + pkg);
  context.uiSetAdvanced(false); await settle();
  if (el("first-run-apps-body").innerHTML.includes('data-app-action="stop"')) fail("switching Technical details off must remove Stop from the DOM");
  await context.appAction("install", "flow"); await settle(); await settle();
  const appPost = calls.find((c) => c.path === "/api/gateway/apps/flow/install");
  if (!appPost || appPost.csrf !== "agcsrf_wizard" || JSON.parse(appPost.body).launch !== true) fail("app install POST wrong: " + JSON.stringify(appPost));
  if (!calls.some((c) => c.path === "/api/gateway/apps/jobs/app-1")) fail("the app install job was not polled");
  if (el("first-run-apps-body").innerHTML.includes("Open it with the Open button")) fail("a finished install never leaves a box restating the pill");

  context.firstRunStep(1); await settle();
  if (!el("first-run-done-body").innerHTML.includes("abstractgateway claim --open")) fail("done step missing the CLI equivalents");
  // The console's own terminal app: one quiet line; its two commands only
  // with Technical details ON (rendered, not hidden by CSS).
  const note = el("first-run-console-tui").innerHTML;
  if (!note.includes("This console also exists as a terminal app") || note.includes("cargo install abstractgateway-console") || note.includes("ui-cmdline")) fail("Done step: one quiet line, no commands: " + note);
  context.uiSetAdvanced(true); await settle();
  const noteTech = el("first-run-console-tui").innerHTML;
  for (const want of ["cargo install abstractgateway-console", "abstractgateway-console --url http://127.0.0.1:18080"]) if (!noteTech.includes(want)) fail("Done step with Technical details must show " + want + ": " + noteTech);
  context.uiSetAdvanced(false); await settle();
  if (hidden("first-run-finish") || !hidden("first-run-next")) fail("finish/next buttons wrong on the last step");

  el("first-run-finish").onclick(); await settle();
  const post = calls.find((c) => c.path === "/api/gateway/host/first-run" && c.method === "POST");
  if (!post) fail("finish did not record the first-run state");
  if (JSON.parse(post.body).outcome !== "finished") fail("finish outcome wrong: " + post.body);
  if (post.csrf !== "agcsrf_wizard") fail("finish POST carried no CSRF header");
  if (!hidden("first-run-backdrop")) fail("wizard stayed open after finish");
  if (scenario.expectMount) {
    // The Models / Engines tabs mount on first open, refresh on re-open.
    el("tab-button-catalog").onclick(); await settle();
    el("tab-button-engines").onclick(); await settle();
    el("tab-button-catalog").onclick(); await settle();
    const tabModels = mounts.filter((x) => x.id === "catalog-core-root");
    if (tabModels.length !== 1 || tabModels[0].kind !== "models") fail("Models tab did not mount once: " + JSON.stringify(mounts.map((x) => [x.kind, x.id])));
    if (mounts.some((x) => x.id === "engines-core-root")) fail("the Engines tab renders console cards, it must not mount AbstractCore's table");
    if (!el("engines-core-root").innerHTML.includes("data-engine-card")) fail("Engines tab shows no engine cards: " + el("engines-core-root").innerHTML);
    if (tabModels[0].refreshed !== 1) fail("re-opening the Models tab must refresh it");
    if (tabModels[0].options.cliPrefix !== "abstractgateway" || tabModels[0].options.apiBase !== "/api/gateway") fail("tab mount options wrong");
    if (!String(el("tab-catalog").className).includes("active")) fail("Models tab panel not active");
    // The tab shows the catalog cards above AbstractCore's list, and its
    // filters are the shareable `#catalog` link.
    if (!el("catalog-cards-root").innerHTML.includes('data-mc-model="big-model"')) fail("the Models tab opens on the whole catalog (no fits preset): " + el("catalog-cards-root").innerHTML);
    if (!String(history.calls[history.calls.length - 1].url).endsWith("#catalog")) fail("the Models tab did not write its #catalog link: " + JSON.stringify(history.calls.slice(-2)));
  } else {
    el("tab-button-catalog").onclick(); await settle();
    el("tab-button-engines").onclick(); await settle();
    if (mounts.length) fail("nothing may mount without the AbstractCore screens");
  }
}

if (scenario.name === "claim-tab") {
  // `#claim=<code>&tab=<tab>`: the code is stripped, the tab survives as the
  // `#<tab>` deep link, and the guide does not cover the requested tab.
  if (!history.calls.length) fail("claim code was not stripped from the URL");
  const url = String(history.calls[0].url);
  if (url.includes("claim")) fail("replaceState kept the claim: " + url);
  if (!url.endsWith("#" + scenario.tab)) fail("the requested tab was dropped: " + url);
  if (!String(document.body.className).includes("signed-in")) fail("claim did not sign in");
  if (!String(el("tab-" + scenario.tab).className).includes("active")) fail("tab " + scenario.tab + " is not the active tab: " + el("tab-" + scenario.tab).className);
  if (!hidden("first-run-backdrop")) fail("the guide opened over the requested tab");
}
if (scenario.name === "claim-no-guide") {
  // A claim after first run is completed (every tray "Open Console") or a
  // tray link (claim.created_by "tray") never auto-opens the guide.
  if (!String(document.body.className).includes("signed-in")) fail("claim did not sign in");
  if (!hidden("first-run-backdrop")) fail("the guide auto-opened on a claim (completed=" + scenario.completed + ", created_by=" + scenario.createdBy + ")");
  if (hidden("open-setup")) fail("Setup button must stay reachable");
  el("open-setup").onclick(); await settle();
  if (hidden("first-run-backdrop")) fail("Setup button did not reopen the guide");
}

if (scenario.name === "create-user") {
  // Mission BB/X2: with user accounts off only admin accounts can sign in, so
  // the create-user modal offers admin alone and says why; `/me` without the
  // field is said out loud; a 409 that still comes back is shown word for word.
  loggedIn = true;
  vm.runInContext("refresh()", context);
  await settle();
  const opt = (value) => ({ value, hidden: false, disabled: false });
  el("new-roles").children = [opt("user"), opt("admin"), opt("readonly")];
  el("new-roles").value = "user";
  el("new-user").focus = () => {};
  context.openUserCreate();
  const shown = el("new-roles").children.filter((o) => !o.hidden && !o.disabled).map((o) => o.value);
  const note = el("new-roles-note");
  if (JSON.stringify(shown) !== JSON.stringify(scenario.wantRoles)) fail("roles offered " + JSON.stringify(shown) + ", want " + JSON.stringify(scenario.wantRoles));
  if (scenario.wantRoles.length === 1 && el("new-roles").value !== "admin") fail("accounts off must preselect admin: " + el("new-roles").value);
  if (scenario.wantNote && !note.textContent.includes(scenario.wantNote)) fail("modal note missing: " + note.textContent);
  if (!scenario.wantNote && !String(note.className).includes("hidden")) fail("no note when accounts are on: " + note.textContent);
  if (scenario.post) {
    el("new-user").value = "bob";
    el("new-roles").value = "user";
    await context.createUser(); await settle();
    if (!el("user-create-message").textContent.includes("user accounts off, so a non-admin account could not sign in")) fail("the server's 409 message must be shown: " + el("user-create-message").textContent);
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


def test_wizard_without_the_screens_script_still_shows_engine_cards() -> None:
    # Server has the screens, but the page's AbstractCore script did not run.
    code = "agclaim_" + "B" * 43
    result = _run({
        "name": "claim", "hash": f"#claim={code}", "code": code, "completed": False, "coreStub": False,
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
        "enginesFail": "Models and Engines require abstractcore \u2265 2.14.0 (this gateway has 2.13.42)",
        "enginesWant": ["cannot list its engines", "require abstractcore \u2265 2.14.0", "2.13.42", 'pip install -U &quot;abstractcore&gt;=2.14.0&quot;', "https://ollama.com/download", "https://lmstudio.ai/download"],
        "catalogFail": "Models and Engines require abstractcore \u2265 2.14.0 (this gateway has 2.13.42)",
        "catalogWant": "require abstractcore \u2265 2.14.0",
    }, html=html)
    # A global that happens to exist is ignored when the server said the
    # screens are unavailable: the page never mounts (expectMount False).
    assert result.returncode == 0, result.stderr + result.stdout


def test_completed_data_dir_does_not_auto_open_but_setup_reopens() -> None:
    result = _run({"name": "completed", "hash": "", "code": "", "completed": True})
    assert result.returncode == 0, result.stderr + result.stdout
    assert "OK" in result.stdout


def test_claim_link_with_a_tab_lands_on_that_tab_without_the_guide() -> None:
    code = "agclaim_" + "D" * 43
    result = _run({"name": "claim-tab", "hash": f"#claim={code}&tab=apps", "code": code, "completed": False, "tab": "apps"})
    assert result.returncode == 0, result.stderr + result.stdout
    assert "OK" in result.stdout


def test_claim_link_to_the_network_tab() -> None:
    code = "agclaim_" + "E" * 43
    result = _run({"name": "claim-tab", "hash": f"#claim={code}&tab=network", "code": code, "completed": True, "tab": "network"})
    assert result.returncode == 0, result.stderr + result.stdout


def test_claim_after_first_run_completed_does_not_open_the_guide() -> None:
    code = "agclaim_" + "F" * 43
    result = _run({"name": "claim-no-guide", "hash": f"#claim={code}", "code": code, "completed": True})
    assert result.returncode == 0, result.stderr + result.stdout
    assert "OK" in result.stdout


def test_tray_claim_never_opens_the_guide_even_before_completion() -> None:
    code = "agclaim_" + "G" * 43
    result = _run({"name": "claim-no-guide", "hash": f"#claim={code}", "code": code, "completed": False, "createdBy": "tray"})
    assert result.returncode == 0, result.stderr + result.stdout


@pytest.mark.parametrize(
    "me_auth,want_roles,want_note,post",
    [
        ({"mode": "legacy-token", "user_auth_enabled": False}, ["admin"], "User accounts are off on this gateway: only admin accounts can sign in. Turn user accounts on to add members.", False),
        ({"mode": "users", "user_auth_enabled": True}, ["user", "admin", "readonly"], "", False),
        (None, ["user", "admin", "readonly"], "did not say whether user accounts are on", True),
    ],
    ids=["accounts-off", "accounts-on", "field-absent"],
)
def test_create_user_modal_follows_the_user_accounts_mode(me_auth, want_roles, want_note, post) -> None:
    result = _run({"name": "create-user", "hash": "", "code": "", "completed": True, "meAuth": me_auth, "wantRoles": want_roles, "wantNote": want_note, "post": post})
    assert result.returncode == 0, result.stderr + result.stdout
    assert "OK" in result.stdout
