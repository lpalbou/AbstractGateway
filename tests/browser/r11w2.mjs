// Browser checks of the round-11 console Workspaces modals (DESIGN.md R11.1 FINAL / R11.2), run by
// tests/test_gateway_console_browser_r11w2.py against a hermetic scratch gateway (admin, alice, bob,
// the entity castor) and <dirs>/{projects,notes,secrets,lab,pictures}.
//
//   node r11w2.mjs <base-url> <admin-token> <playwright-node-modules> <dirs> <alice-token> [real|stub] [shots-dir]
//
// "real" (default) drives the gateway's own routes (GET/PUT /workspace/policy[/{account}]).
// "stub" answers /api/gateway/workspace/** from an in-page fake of the R11 WORKSPACE API — FINAL
// shapes (used while the gateway lane's routes were unmerged). Same checks either way.
// With a shots dir: 1440/834/390 x light/dark of Accounts, Eligible workspaces, alice's modal (a
// disabled above-cap control with its tooltip), castor's modal and a refusal; page overflow measured.
// Prints one JSON line: {"failures": [...], "checks": N, "shots": [...]}.
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";

const [BASE, ADMIN, PW, DIRS, ALICE, MODE = "real", SHOTS = ""] = process.argv.slice(2);
const require = createRequire(path.join(PW, "/"));
const { chromium } = require("playwright-core");
const D = (name) => path.join(DIRS, name);
const STUB = MODE === "stub";

const failures = [];
const shots = [];
let checks = 0;
function check(ok, what, detail) {
  checks += 1;
  if (!ok) failures.push(detail === undefined ? what : `${what}: ${JSON.stringify(detail)}`);
}
async function apiAs(token, method, p, body) {
  const r = await fetch(`${BASE}/api/gateway${p}`, { method, headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });
  return { status: r.status, body: await r.json().catch(() => ({})) };
}

// ---------------------------------------------------------------- the stub (R11 API shapes)
const LABEL = { allowed_only: "Deny everything, allow listed workspaces", any_except_denied: "Allow everything, refuse listed workspaces" };
const WORD = { ro: "ro", rw: "rw", deny: "refused" };
const RANK = { deny: 0, ro: 1, rw: 2 };
const fake = {
  gateway: { posture: "any_except_denied", default_mode: "rw", folders: [], builtin_refused: ["/srv/stub-gateway-data"] },
  accounts: {},
};
const line = (posture, dm, rows) => [`${LABEL[posture]}${posture === "any_except_denied" ? ` (${dm})` : ""}`, ...rows.map((r) => `${r.path} (${WORD[r.mode]})`)].join(" · ");
const under = (p, root) => p === root || p.startsWith(root.endsWith("/") ? root : `${root}/`);
function capOf(p) {
  const g = fake.gateway;
  const hits = g.folders.filter((r) => under(p, r.path)).sort((a, b) => b.path.length - a.path.length);
  if (hits.length) return hits[0].mode;
  return g.posture === "any_except_denied" ? g.default_mode : "deny";
}
const gwPolicy = () => ({ ...fake.gateway, folders: fake.gateway.folders.map((r) => ({ ...r })), summary: line(fake.gateway.posture, fake.gateway.default_mode, fake.gateway.folders) });
function accountAnswer(key) {
  const a = fake.accounts[key] || { configured: false };
  const g = fake.gateway;
  const policy = a.configured ? { account: key, configured: true, posture: a.posture, default_mode: a.default_mode, folders: a.folders } : { account: key, configured: false, posture: g.posture, default_mode: g.default_mode, folders: [] };
  const rows = a.configured ? a.folders.map((r) => ({ ...r, cap: capOf(r.path), source: "account" })) : g.folders.map((r) => ({ ...r, cap: r.mode, source: "gateway" }));
  const posture = a.configured ? a.posture : g.posture, dm = a.configured ? a.default_mode : g.default_mode;
  return { ok: true, policy, gateway: gwPolicy(), effective: { ok: true, account: key, session_id: null, level: a.configured ? "account" : "gateway", posture, default_mode: dm, folders: rows, summary: line(posture, dm, rows), gateway_summary: gwPolicy().summary }, can_edit: true };
}
const refuse = (message, p = null) => ({ status: 400, body: { detail: { reason: "workspace_refused", message, path: p } } });
const EXISTS = new Set(["projects", "notes", "secrets", "lab", "pictures"].map(D));
function stubHandle(method, url, body, who) {
  const u = new URL(url);
  const p = u.pathname.replace(/^\/api\/gateway/, "");
  if (p === "/workspace/policy") {
    if (method === "GET") return { status: 200, body: { ok: true, policy: gwPolicy() } };
    if ("shared_workspace" in body) return refuse("shared_workspace no longer exists: list it as a workspace (folders: [{path, mode: \"rw\"}]). Nothing was saved.");
    for (const r of body.folders || []) if (!EXISTS.has(r.path)) return refuse(`${r.path} is not an existing directory on the gateway host.`, r.path);
    Object.assign(fake.gateway, { ...(body.posture ? { posture: body.posture } : {}), ...(body.default_mode ? { default_mode: body.default_mode } : {}), ...(body.folders ? { folders: body.folders } : {}) });
    return { status: 200, body: { ok: true, policy: gwPolicy() } };
  }
  const m = /^\/workspace\/policy\/([^/]+)$/.exec(p);
  if (m) {
    let key = decodeURIComponent(m[1]);
    if (key === "me") key = `default:${who}`;
    if (method === "GET") return { status: 200, body: accountAnswer(key) };
    if (body.configured === false) { fake.accounts[key] = { configured: false }; return { status: 200, body: accountAnswer(key) }; }
    for (const r of body.folders || []) {
      if (!EXISTS.has(r.path)) return refuse(`${r.path} is not an existing directory on the gateway host.`, r.path);
      if (r.mode === "deny") continue;
      const cap = capOf(r.path);
      if (cap === "deny") return refuse(`${r.path} is outside the gateway's eligible workspaces.`, r.path);
      if (RANK[r.mode] > RANK[cap]) return refuse(`The gateway allows ${r.path} read-only.`, r.path);
    }
    const g = fake.gateway;
    fake.accounts[key] = { configured: true, posture: body.posture || g.posture, default_mode: body.default_mode || g.default_mode, folders: body.folders || [] };
    return { status: 200, body: accountAnswer(key) };
  }
  return null;
}

// ---------------------------------------------------------------- browser helpers
async function open(browser, { width = 1440, height = 900, user = "admin", token = ADMIN, theme = null } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 1 });
  if (theme) await ctx.addInitScript((t) => { try { localStorage.setItem("abstractgateway_ui_settings_v1", JSON.stringify({ theme: t })); } catch {} }, theme);
  if (STUB) {
    await ctx.route("**/api/gateway/workspace/**", async (route) => {
      const req = route.request();
      let body = {};
      try { body = req.postDataJSON() || {}; } catch {}
      const out = stubHandle(req.method(), req.url(), body, user);
      if (!out) return route.continue();
      await route.fulfill({ status: out.status, contentType: "application/json", body: JSON.stringify(out.body) });
    });
  }
  const page = await ctx.newPage();
  const puts = [];
  page.on("request", (r) => { if (r.method() === "PUT" && /\/api\/gateway\/workspace\//.test(r.url())) puts.push({ url: new URL(r.url()).pathname, body: r.postDataJSON() }); });
  page.on("pageerror", (e) => failures.push(`pageerror@${width}/${user}: ${e.message}`));
  await page.goto(`${BASE}/console`);
  await page.waitForSelector("#login-form");
  await page.fill("#login-user", user);
  await page.fill("#login-token", token);
  await page.click("#login-button");
  await page.waitForFunction(() => document.body.classList.contains("signed-in"), null, { timeout: 20000 });
  await page.keyboard.press("Escape").catch(() => {});
  await page.evaluate(() => document.body.classList.remove("nav-open"));
  await page.waitForSelector("#users-table tr.accounts-row", { timeout: 15000 });
  await page.waitForTimeout(300);
  return { ctx, page, puts };
}
// The server's view (stub: the fake; real: the gateway's own GET as admin).
async function serverGateway() { return STUB ? gwPolicy() : (await apiAs(ADMIN, "GET", "/workspace/policy")).body.policy; }
async function serverAccount(key) { return STUB ? accountAnswer(key) : (await apiAs(ADMIN, "GET", `/workspace/policy/${key}`)).body; }
const settle = async (page, puts, n) => { for (let i = 0; i < 40 && puts.length < n; i += 1) await page.waitForTimeout(50); await page.waitForTimeout(250); };
const text = (page, sel) => page.textContent(sel);
const visible = async (page, root) => page.evaluate((r) => document.querySelector(r).innerText, root);
async function shot(page, name) {
  if (!SHOTS) return;
  const m = await page.evaluate(() => {
    const t = document.querySelector(".af-tooltip:not([hidden])");
    const r = t ? t.getBoundingClientRect() : null;
    const modal = document.querySelector(".af-modal-backdrop:not([hidden]) .af-modal__body");
    return { page: document.documentElement.scrollWidth - innerWidth, modal: modal ? modal.scrollWidth - modal.clientWidth : 0, tooltip: t ? { text: t.textContent, inside: r.left >= 0 && r.right <= innerWidth && r.top >= 0 && r.bottom <= innerHeight } : null };
  });
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: path.join(SHOTS, `${name}.png`) });
  shots.push({ file: `${name}.png`, ...m });
  check(m.page <= 0 && m.modal <= 0, `no horizontal scroll: ${name}`, m);
}

const T = {
  gatewayTitle: "Eligible workspaces",
  a: "Deny everything, allow listed workspaces",
  b: "Allow everything, refuse listed workspaces",
  cap: "The gateway allows this workspace read-only",
};
const GW_SEED = { posture: "allowed_only", default_mode: "rw", folders: [{ path: D("projects"), mode: "rw" }, { path: D("notes"), mode: "ro" }] };

const browser = await chromium.launch();
try {
  // Seed the gateway's eligible set (both modes, through the route the console uses).
  if (STUB) Object.assign(fake.gateway, GW_SEED);
  else { const r = await apiAs(ADMIN, "PUT", "/workspace/policy", GW_SEED); check(r.status === 200, "seed: PUT /workspace/policy", r); }

  // ------------------------------------------------------------- G: Eligible workspaces (admin)
  {
    const { ctx, page, puts } = await open(browser);
    const btn = page.locator("#accounts-gw-workspace");
    check(await btn.isVisible() && (await btn.textContent()) === T.gatewayTitle, "admin sees the 'Eligible workspaces' button", await btn.textContent());
    const first = await page.evaluate(() => document.querySelector(".accounts-head__actions > :not([hidden]):not(.hidden)").id);
    check(first === "accounts-gw-workspace", "the button is the first control at the top of Accounts", first);
    await btn.click();
    await page.waitForSelector("#gateway-workspace-backdrop:not([hidden]) [data-workspace='effective']");
    check((await text(page, "#gateway-workspace-title")) === T.gatewayTitle, "modal title 'Eligible workspaces'");
    const B = "#gateway-workspace-body";
    const want = `${T.a} · ${D("projects")} (rw) · ${D("notes")} (ro)`;
    check((await text(page, `${B} [data-workspace='effective']`)) === want && (await serverGateway()).summary === want, "the ceiling line verbatim (= policy.summary)", await text(page, `${B} [data-workspace='effective']`));
    check((await page.locator(`${B} [data-workspace='gateway-line']`).count()) === 0, "no 'Gateway:' line at the gateway level");
    check((await page.locator(`${B} [data-workspace='follow']`).count()) === 0, "no follow switch at the gateway level");
    const postures = await page.evaluate((b) => Array.from(document.querySelectorAll(`${b} [data-action^='workspace-posture-']`)).map((x) => [x.textContent, x.getAttribute("aria-pressed")]), B);
    check(JSON.stringify(postures) === JSON.stringify([[T.a, "true"], [T.b, "false"]]), "posture segmented control, both labels verbatim", postures);
    const rows = await page.evaluate((b) => Array.from(document.querySelectorAll(`${b} li[data-workspace='row']`)).map((r) => [r.dataset.path, r.dataset.mode, r.querySelectorAll("[data-action^='workspace-mode-'][aria-disabled='true']").length]), B);
    check(JSON.stringify(rows) === JSON.stringify([[D("projects"), "rw", 0], [D("notes"), "ro", 0]]), "rows with their caps, no mode disabled (the mode IS the cap)", rows);
    check((await page.locator(`${B} button:text-is('Save')`).count()) === 0, "no Save button");
    const vis = await visible(page, "#gateway-workspace-backdrop .af-modal");
    check(!/shared|folder/i.test(vis), "no 'shared' / 'folder' in the modal", (vis.match(/[^\n]*(shared|folder)[^\n]*/i) || [""])[0]);
    const rm = await page.evaluate((b) => { const x = document.querySelector(`${b} li[data-workspace='row'] [data-action='workspace-remove']`); return x ? { tip: x.dataset.afTip, aria: x.getAttribute("aria-label"), title: x.getAttribute("title") } : null; }, B);
    check(rm && rm.tip === `Remove ${D("projects")}` && rm.aria === rm.tip && rm.title === null, "remove icon: kit tooltip + aria-label, no native title", rm);
    // One change = one PUT (full body).
    let n = puts.length;
    await page.click(`${B} li[data-path='${D("notes")}'] [data-action='workspace-mode-rw']`);
    await settle(page, puts, n + 1);
    check(puts.length === n + 1 && puts.at(-1).url === "/api/gateway/workspace/policy" && JSON.stringify(puts.at(-1).body) === JSON.stringify({ posture: "allowed_only", default_mode: "rw", folders: [{ path: D("projects"), mode: "rw" }, { path: D("notes"), mode: "rw" }] }), "a mode change is ONE PUT of the full body", puts.slice(n));
    check((await serverGateway()).folders.find((r) => r.path === D("notes")).mode === "rw", "the cap is stored");
    check((await text(page, `${B} [data-workspace='effective']`)).endsWith(`${D("notes")} (rw)`), "the line follows the answer");
    // Add: a new row starts Read & write under 'Deny everything…'.
    n = puts.length;
    await page.fill(`${B} [data-workspace='add'] input`, D("pictures"));
    await page.keyboard.press("Enter");
    await settle(page, puts, n + 1);
    check(puts.length === n + 1 && (await serverGateway()).folders.some((r) => r.path === D("pictures") && r.mode === "rw"), "Add a workspace path: one PUT, Read & write at the gateway", (await serverGateway()).folders);
    // Refusal: a missing directory -> the gateway's sentence + Not saved., nothing stored.
    n = puts.length;
    await page.fill(`${B} [data-workspace='add'] input`, D("missing-dir"));
    await page.click(`${B} [data-action='workspace-add']`);
    await settle(page, puts, n + 1);
    const refusal = await page.evaluate((b) => { const x = document.querySelector(`${b} [data-workspace='add'] [data-workspace='refusal']`); return x ? x.textContent : null; }, B);
    check(!!refusal && refusal.endsWith("Not saved.") && refusal.includes(D("missing-dir")), "a refused path: the gateway's sentence + 'Not saved.'", refusal);
    check(!(await serverGateway()).folders.some((r) => r.path === D("missing-dir")), "a refused path is not stored");
    if (SHOTS) await shot(page, "gateway-refusal-1440-light");
    // Remove + posture.
    n = puts.length;
    await page.click(`${B} li[data-path='${D("pictures")}'] [data-action='workspace-remove']`);
    await settle(page, puts, n + 1);
    check(!(await serverGateway()).folders.some((r) => r.path === D("pictures")), "remove: one PUT");
    n = puts.length;
    await page.click(`${B} [data-action='workspace-posture-any_except_denied']`);
    await settle(page, puts, n + 1);
    const g2 = await serverGateway();
    check(g2.posture === "any_except_denied" && (await page.locator(`${B} [data-workspace='everything-else']`).count()) === 1, "the posture applies at once; Everything else appears", g2.posture);
    check((await text(page, `${B} [data-workspace='effective']`)) === g2.summary, "line verbatim after the posture change", [await text(page, `${B} [data-workspace='effective']`), g2.summary]);
    // Back to the seed.
    if (STUB) Object.assign(fake.gateway, GW_SEED); else await apiAs(ADMIN, "PUT", "/workspace/policy", GW_SEED);
    await page.click("#gateway-workspace-close");
    await ctx.close();
  }

  // ------------------------------------------------------------- A: one account (alice), the account level
  {
    const { ctx, page, puts } = await open(browser);
    const rowsWith = await page.evaluate(() => Array.from(document.querySelectorAll("#users-table tr.accounts-row")).filter((tr) => tr.querySelector("button[data-action='workspace']")).map((tr) => tr.dataset.user).sort());
    check(["admin", "alice", "bob", "castor"].every((u) => rowsWith.includes(u)), "the workspace icon on every live row (users, entity, the admin's own)", rowsWith);
    const tip = await page.getAttribute("tr[data-user='castor'] button[data-action='workspace']", "data-af-tip");
    check(tip === "Workspaces castor's agents may use", "entity row: kit tooltip sentence", tip);
    await page.click("tr[data-user='alice'] button[data-action='workspace']");
    const B = "#account-workspace-body";
    await page.waitForSelector(`${B} [data-workspace='effective'], ${B} [data-workspace='load-error']`);
    check((await page.locator(`${B} [data-workspace='load-error']`).count()) === 0, "alice's modal loads", await page.locator(`${B} [data-workspace='load-error']`).allTextContents());
    check((await text(page, "#account-workspace-title")) === "Workspaces — alice", "the modal names the account");
    let s = await serverAccount("default:alice");
    check((await text(page, `${B} [data-workspace='gateway-line']`)) === `Gateway: ${s.effective.gateway_summary}`, "top line 'Gateway: <gateway_summary>' verbatim", await text(page, `${B} [data-workspace='gateway-line']`));
    check((await text(page, `${B} [data-workspace='effective']`)) === s.effective.summary, "effective line verbatim", [await text(page, `${B} [data-workspace='effective']`), s.effective.summary]);
    const sw = `${B} [data-action='workspace-follow-gateway']`;
    check((await page.getAttribute(sw, "aria-checked")) === "true" && s.policy.configured === false, "Follow the gateway policy: ON while not configured");
    check((await page.locator(`${B} [data-action^='workspace-mode-']`).count()) === 0 && (await page.locator(`${B} [data-workspace='add']`).count()) === 0, "following: rows read-only, no add row");
    // OFF -> one PUT configured:true starting from what applies.
    let n = puts.length;
    await page.click(sw);
    await settle(page, puts, n + 1);
    check(puts.length === n + 1 && puts.at(-1).body.configured === true && JSON.stringify(puts.at(-1).body.folders) === JSON.stringify(s.effective.folders.map((f) => ({ path: f.path, mode: f.mode }))), "switch OFF: ONE PUT {configured:true, …effective rows}", puts.slice(n));
    s = await serverAccount("default:alice");
    check(s.policy.configured === true && (await page.getAttribute(sw, "aria-checked")) === "false", "now configured; switch shows OFF");
    // The above-cap control: disabled, focusable, kit tooltip on focus; a click sends nothing.
    const capSel = `${B} li[data-path='${D("notes")}'] [data-action='workspace-mode-rw']`;
    const cap = await page.evaluate((sel) => { const b = document.querySelector(sel); return b ? { dis: b.getAttribute("aria-disabled"), tip: b.dataset.afTip, disabledAttr: b.disabled } : null; }, capSel);
    check(cap && cap.dis === "true" && cap.tip === T.cap && cap.disabledAttr === false, "above the cap: aria-disabled + kit tooltip verbatim, still focusable", cap);
    n = puts.length;
    await page.click(capSel, { force: true });
    await page.waitForTimeout(300);
    check(puts.length === n, "clicking an above-cap mode sends nothing");
    await page.focus(`${B} li[data-path='${D("notes")}'] [data-action='workspace-mode-ro']`);
    await page.keyboard.press("Shift+Tab");
    await page.waitForTimeout(300);
    const shown = await page.evaluate(() => { const t = document.querySelector(".af-tooltip"); return t && !t.hidden ? t.textContent : null; });
    check(shown === T.cap, "keyboard focus shows the cap tooltip", shown);
    if (SHOTS) await shot(page, "account-cap-tooltip-1440-light");
    // Lower projects -> one PUT; the line follows.
    n = puts.length;
    await page.mouse.move(2, 2);
    await page.click(`${B} li[data-path='${D("projects")}'] [data-action='workspace-mode-ro']`);
    await settle(page, puts, n + 1);
    s = await serverAccount("default:alice");
    check(puts.length === n + 1 && s.policy.folders.find((r) => r.path === D("projects")).mode === "ro" && (await text(page, `${B} [data-workspace='effective']`)) === s.effective.summary, "a mode change: one PUT, line verbatim", s.effective.summary);
    // A path outside the eligible set: the gateway's sentence + Not saved.
    n = puts.length;
    await page.fill(`${B} [data-workspace='add'] input`, D("secrets"));
    await page.click(`${B} [data-action='workspace-add']`);
    await settle(page, puts, n + 1);
    const ref = await page.evaluate((b) => { const x = document.querySelector(`${b} [data-workspace='add'] [data-workspace='refusal']`); return x ? x.textContent : null; }, B);
    check(!!ref && ref.endsWith(" Not saved.") && ref.includes(D("secrets")), "outside the eligible set: refused inline with the gateway's sentence", ref);
    check(!(await serverAccount("default:alice")).policy.folders.some((r) => r.path === D("secrets")), "the refused path is not stored");
    if (SHOTS) await shot(page, "account-refusal-1440-light");
    // ON again -> {configured:false}.
    n = puts.length;
    await page.click(sw);
    await settle(page, puts, n + 1);
    check(puts.length === n + 1 && JSON.stringify(puts.at(-1).body) === JSON.stringify({ configured: false }) && (await serverAccount("default:alice")).policy.configured === false, "Follow the gateway policy ON: ONE PUT {configured:false}", puts.slice(n));
    await page.click("#account-workspace-close");
    // The entity's modal (admin) and the admin's own (me).
    for (const [who, key] of [["castor", "default:castor"], ["admin", "me"]]) {
      const before = puts.length;
      await page.click(`tr[data-user='${who}'] button[data-action='workspace']`);
      await page.waitForSelector(`${B} [data-workspace='effective'], ${B} [data-workspace='load-error']`);
      const err = await page.locator(`${B} [data-workspace='load-error']`).allTextContents();
      check(err.length === 0, `${who}'s modal loads (${key})`, err);
      check(puts.length === before, `${who}: opening sends no PUT`);
      await page.click("#account-workspace-close");
    }
    await ctx.close();
  }

  // ------------------------------------------------------------- U: a non-admin opens their own
  {
    const { ctx, page, puts } = await open(browser, { user: "alice", token: ALICE });
    check(!(await page.locator("#accounts-gw-workspace").isVisible()), "a user does not see 'Eligible workspaces'");
    await page.click("tr[data-user='alice'] button[data-action='workspace']");
    const B = "#account-workspace-body";
    await page.waitForSelector(`${B} [data-workspace='effective'], ${B} [data-workspace='load-error']`);
    check((await page.locator(`${B} [data-workspace='load-error']`).count()) === 0, "a user opens their own workspaces (me)");
    const n = puts.length;
    await page.click(`${B} [data-action='workspace-follow-gateway']`);
    await settle(page, puts, n + 1);
    check(puts.length === n + 1 && puts.at(-1).url === "/api/gateway/workspace/policy/me", "a user's change goes to /workspace/policy/me", puts.slice(n));
    await page.click(`${B} [data-action='workspace-follow-gateway']`);
    await settle(page, puts, n + 2);
    await page.click("#account-workspace-close");
    await ctx.close();
  }

  // ------------------------------------------------------------- S: screenshots
  if (SHOTS) {
    // alice configured with a read-only-capped row, so her modal shows the disabled control.
    const aliceCfg = { configured: true, posture: "allowed_only", default_mode: "rw", folders: [{ path: D("projects"), mode: "rw" }, { path: D("notes"), mode: "ro" }] };
    if (STUB) fake.accounts["default:alice"] = aliceCfg; else await apiAs(ADMIN, "PUT", "/workspace/policy/default:alice", aliceCfg);
    for (const theme of ["light", "dark"]) {
      for (const [w, h] of [[1440, 1000], [834, 1194], [390, 844]]) {
        const tag = `${w}-${theme}`;
        const { ctx, page } = await open(browser, { width: w, height: h, theme });
        await shot(page, `accounts-${tag}`);
        await page.click("#accounts-gw-workspace");
        await page.waitForSelector("#gateway-workspace-backdrop:not([hidden]) [data-workspace='effective']");
        await page.waitForTimeout(250);
        await shot(page, `eligible-workspaces-${tag}`);
        await page.click("#gateway-workspace-close");
        await page.locator("tr[data-user='alice'] button[data-action='workspace']").scrollIntoViewIfNeeded();
        await page.click("tr[data-user='alice'] button[data-action='workspace']");
        await page.waitForSelector("#account-workspace-body [data-workspace='effective']");
        const capSel = `#account-workspace-body li[data-path='${D("notes")}'] [data-action='workspace-mode-rw']`;
        await page.locator(capSel).scrollIntoViewIfNeeded();
        await page.focus(`#account-workspace-body li[data-path='${D("notes")}'] [data-action='workspace-mode-ro']`);
        await page.keyboard.press("Shift+Tab");
        await page.waitForTimeout(350);
        await shot(page, `account-alice-cap-tooltip-${tag}`);
        await page.fill("#account-workspace-body [data-workspace='add'] input", D("secrets"));
        await page.click("#account-workspace-body [data-action='workspace-add']");
        await page.waitForSelector("#account-workspace-body [data-workspace='add'] [data-workspace='refusal']");
        await page.locator("#account-workspace-body [data-workspace='add']").scrollIntoViewIfNeeded();
        await page.waitForTimeout(200);
        await shot(page, `account-alice-refusal-${tag}`);
        await page.click("#account-workspace-close");
        await page.locator("tr[data-user='castor'] button[data-action='workspace']").scrollIntoViewIfNeeded();
        await page.click("tr[data-user='castor'] button[data-action='workspace']");
        await page.waitForSelector("#account-workspace-body [data-workspace='effective']");
        await page.waitForTimeout(250);
        await shot(page, `account-entity-castor-${tag}`);
        await page.click("#account-workspace-close");
        await ctx.close();
      }
    }
  }
} catch (e) {
  failures.push(`exception: ${e && e.stack ? e.stack : e}`);
} finally {
  await browser.close();
}
if (SHOTS) fs.writeFileSync(path.join(SHOTS, "report.json"), JSON.stringify({ mode: MODE, failures, shots }, null, 2));
console.log(JSON.stringify({ mode: MODE, failures, checks, shots: shots.length }));
