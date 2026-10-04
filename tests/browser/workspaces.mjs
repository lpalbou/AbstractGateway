// Browser checks of the Workspaces page, the Accounts -> Workspaces / Runtimes links and the Runtimes
// account filter (DESIGN.md round 8, R8.2), run by tests/test_gateway_console_browser_workspaces.py
// against a hermetic scratch gateway (admin, alice with her own policy, bob, the entity castor; the
// gateway allows <folders>/projects + <folders>/notes and refuses <folders>/secrets).
//
//   node workspaces.mjs <base-url> <admin-token> <playwright-node-modules> <folders-root> <alice-token>
//
// Prints one JSON line: {"failures": [...], "checks": N}.
import { createRequire } from "node:module";
import path from "node:path";

const [BASE, ADMIN, PW, FOLDERS, ALICE] = process.argv.slice(2);
const require = createRequire(path.join(PW, "/"));
const { chromium } = require("playwright-core");

const failures = [];
let checks = 0;
function check(ok, what, detail) {
  checks += 1;
  if (!ok) failures.push(detail === undefined ? what : `${what}: ${JSON.stringify(detail)}`);
}
async function api(method, p, body, token = ADMIN) {
  const r = await fetch(`${BASE}/api/gateway${p}`, { method, headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });
  return { status: r.status, body: await r.json().catch(() => ({})) };
}
const cfg = async () => (await api("GET", "/admin/runtime-config")).body;
const sorted = (a) => JSON.stringify([...(a || [])].sort());
const policy = async (user) => (await api("GET", `/admin/user-workspace-policy?tenant_id=default&user_id=${user}`)).body;

async function open(browser, width, height, { hash = "", user = "admin", token = ADMIN, touch = false } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height }, hasTouch: touch, isMobile: touch });
  const page = await ctx.newPage();
  page.on("pageerror", (e) => failures.push(`pageerror@${width}: ${e.message}`));
  await page.goto(`${BASE}/console${hash}`);
  await page.waitForSelector("#login-form");
  await page.fill("#login-user", user);
  await page.fill("#login-token", token);
  await page.click("#login-button");
  await page.waitForFunction(() => document.body.classList.contains("signed-in"), null, { timeout: 20000 });
  await page.keyboard.press("Escape").catch(() => {});
  await page.evaluate(() => document.body.classList.remove("nav-open"));
  return { ctx, page };
}
async function gotoWorkspaces(page) {
  await page.evaluate(() => document.getElementById("tab-button-workspaces").click());
  await page.waitForSelector("#workspaces-root [data-ws-summary]", { timeout: 15000 });
}
const savedNear = (page, sel) => page.evaluate((s) => { const f = document.querySelector(s).closest(".ws-field"); return f ? f.querySelector(".ws-saved").textContent : null; }, sel);

const browser = await chromium.launch({ headless: true });
try {
  // ---------------------------------------------------------------- sidebar + Accounts no longer carries the policy
  {
    const { ctx, page } = await open(browser, 1440, 1000);
    const nav = await page.evaluate(() => Array.from(document.querySelectorAll(".shell_nav .tab-button")).map((b) => b.id));
    check(nav.indexOf("tab-button-workspaces") === nav.indexOf("tab-button-users") + 1, "Workspaces sits right after Accounts in the sidebar", nav);
    check((await page.textContent("#tab-button-workspaces .shell_nav_label")).trim() === "Workspaces", "the entry is called Workspaces");
    await page.evaluate(() => document.getElementById("tab-button-users").click());
    await page.waitForSelector("#users-table tr[data-user='alice']");
    const acc = await page.evaluate(() => ({ disclosure: !!document.getElementById("my-workspace-policy-section"), modal: !!document.getElementById("workspace-policy-modal-backdrop"), text: document.getElementById("tab-users").innerText }));
    check(!acc.disclosure && !acc.modal && !/Workspace policy|Launch-folder trust|Refused folders/.test(acc.text), "the Accounts page carries no workspace policy (no disclosure, no modal, no fields)", { disclosure: acc.disclosure, modal: acc.modal });
    // The Workspace icon on alice's row opens the Workspaces page focused on her row.
    await page.click("tr[data-user='alice'] button[data-action='workspace']");
    await page.waitForSelector("#tab-workspaces.active [data-ws-account='alice'].is-focus", { timeout: 15000 });
    check((await page.evaluate(() => location.hash)) === "#workspaces?account=alice", "Accounts Workspace icon -> #workspaces?account=alice", await page.evaluate(() => location.hash));
    await ctx.close();
  }
  // ---------------------------------------------------------------- the gateway policy (admin)
  {
    const { ctx, page } = await open(browser, 1440, 1000);
    await gotoWorkspaces(page);
    const sum = await page.evaluate(() => { const el = document.querySelector("[data-ws-summary]"); const cs = getComputedStyle(el); const lh = parseFloat(cs.lineHeight) || 20; const pad = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom); return { text: el.textContent, lines: Math.round((el.getBoundingClientRect().height - pad) / lh), top: el.getBoundingClientRect().top < document.querySelector("[data-ws-scope='gateway']").getBoundingClientRect().top }; });
    check(sum.lines === 1 && sum.top && sum.text === "Agents may use only 2 allowed folders and the folder they start in; 1 folder refused. 1 account has its own policy.", "the effective summary: one line, at the top", sum);
    check((await page.locator("#tab-workspaces button", { hasText: /^\s*Save/ }).count()) === 0, "no Save button anywhere on the page");
    // Access mode: a segmented switch with the two options; a click applies at once.
    const seg = await page.evaluate(() => { const g = document.querySelector("[data-ws-scope='gateway'] [role=radiogroup]"); return { label: g.getAttribute("aria-label"), opts: Array.from(g.querySelectorAll("[role=radio]")).map((r) => [r.querySelector(".ui-seg__title").textContent, r.getAttribute("aria-checked")]) }; });
    check(JSON.stringify(seg) === JSON.stringify({ label: "Access", opts: [["Allow my list", "true"], ["Allow everything except", "false"]] }), "access mode = segmented switch Allow my list | Allow everything except", seg);
    await page.click("#ws-gw-mode-blacklist");
    await page.waitForFunction(() => document.getElementById("ws-gw-mode-blacklist").getAttribute("aria-checked") === "true", null, { timeout: 10000 });
    check((await cfg()).workspace_default_mode.value === "blacklist" && (await savedNear(page, "#ws-gw-mode-blacklist")) === "Saved", "mode click -> POST /admin/runtime-config, inline Saved");
    check((await page.textContent("[data-ws-summary]")).startsWith("Agents may use any folder except 1 refused folder."), "the summary follows the change", await page.textContent("[data-ws-summary]"));
    await page.click("#ws-gw-mode-whitelist");
    await page.waitForFunction(() => document.getElementById("ws-gw-mode-whitelist").getAttribute("aria-checked") === "true", null, { timeout: 10000 });
    // Launch-folder trust: a real switch.
    check((await page.getAttribute("#ws-gw-trust", "role")) === "switch" && (await page.getAttribute("#ws-gw-trust", "aria-checked")) === "true", "launch-folder trust is a switch, on");
    await page.click("#ws-gw-trust");
    await page.waitForFunction(() => document.getElementById("ws-gw-trust").getAttribute("aria-checked") === "false" && !document.getElementById("ws-gw-trust").hasAttribute("aria-busy"), null, { timeout: 10000 });
    check((await cfg()).trust_client_launch_folder.value === false, "trust off -> stored");
    await page.click("#ws-gw-trust");
    await page.waitForFunction(() => document.getElementById("ws-gw-trust").getAttribute("aria-checked") === "true" && !document.getElementById("ws-gw-trust").hasAttribute("aria-busy"), null, { timeout: 10000 });
    // Allowed folders: rows; invalid paths say why and are NOT saved.
    const rows = await page.$$eval("#ws-gw-allowed input", (els) => els.map((e) => e.value));
    check(sorted(rows) === sorted([`${FOLDERS}/projects`, `${FOLDERS}/notes`]), "allowed folders are editable rows", rows);
    for (const [typed, sentence] of [["relative/folder", "Use a full path that starts with / (or ~ for the gateway's home folder). Not saved."], [`${FOLDERS}/does-not-exist`, "No folder at this path on the gateway's computer. Not saved."], [`${FOLDERS}/a-file.txt`, "This is a file, not a folder. Not saved."]]) {
      await page.click("[data-ws-add='ws-gw-allowed']");
      const input = page.locator("#ws-gw-allowed li:last-child input");
      await input.fill(typed);
      await input.press("Enter");
      await page.waitForFunction(() => /Not saved/.test(document.querySelector("#ws-gw-allowed li:last-child .ws-folder__state").textContent), null, { timeout: 10000 });
      const st = await page.textContent("#ws-gw-allowed li:last-child .ws-folder__state");
      check(st === sentence && (await input.getAttribute("aria-invalid")) === "true", `invalid path '${typed}': its sentence`, st);
      check(sorted((await cfg()).workspace_allowed_paths.paths) === sorted([`${FOLDERS}/projects`, `${FOLDERS}/notes`]), `invalid path '${typed}' is not saved`);
      await input.fill("");
      await input.press("Enter");
    }
    // A valid folder: nothing is written while typing; the blur applies it, with "Saved".
    await page.click("[data-ws-add='ws-gw-allowed']");
    const input = page.locator("#ws-gw-allowed li:last-child input");
    await input.fill(`${FOLDERS}/extra`);
    await page.waitForTimeout(600);
    check(!(await cfg()).workspace_allowed_paths.paths.includes(`${FOLDERS}/extra`), "typing writes nothing (before blur)");
    await page.locator("[data-ws-summary]").click();  // blur
    await page.waitForFunction(() => document.querySelector("#ws-gw-allowed li:last-child .ws-folder__state").textContent === "Saved", null, { timeout: 10000 });
    check(sorted((await cfg()).workspace_allowed_paths.paths) === sorted([`${FOLDERS}/projects`, `${FOLDERS}/notes`, `${FOLDERS}/extra`]), "blur -> the folder is saved");
    // Remove: the row goes, the list is written.
    await page.click("#ws-gw-allowed li:last-child [data-ws-remove]");
    await page.waitForFunction(() => document.querySelectorAll("#ws-gw-allowed li").length === 2, null, { timeout: 10000 });
    check(!(await cfg()).workspace_allowed_paths.paths.includes(`${FOLDERS}/extra`), "remove -> the folder leaves the stored list");
    // Refused folders: the same rows.
    await page.click("[data-ws-add='ws-gw-blocked']");
    const b = page.locator("#ws-gw-blocked li:last-child input");
    await b.fill(`${FOLDERS}/private`);
    await b.press("Enter");
    await page.waitForFunction(() => document.querySelector("#ws-gw-blocked li:last-child .ws-folder__state").textContent === "Saved", null, { timeout: 10000 });
    check((await cfg()).workspace_blocked_paths.paths.includes(`${FOLDERS}/private`), "refused folder saved on Enter (blur)");
    await ctx.close();
  }
  // ---------------------------------------------------------------- per-account policies (admin)
  {
    const { ctx, page } = await open(browser, 1440, 1000, { hash: "#workspaces?account=alice" });
    await page.waitForSelector("[data-ws-account='alice'].is-focus", { timeout: 15000 });
    const alice = await page.evaluate(() => { const r = document.querySelector("[data-ws-account='alice']"); return { on: document.getElementById("ws-own-default-alice").getAttribute("aria-checked"), label: r.querySelector(".af-switch__label").textContent, allowed: Array.from(r.querySelectorAll("[id$='-allowed'] input")).map((i) => i.value), summary: r.querySelector(".ws-acc__summary").textContent }; });
    check(alice.on === "true" && alice.label === "Own policy" && JSON.stringify(alice.allowed) === JSON.stringify([`${FOLDERS}/alice-lab`]) && alice.summary === "Only the gateway's allowed folders, 1 folder of its own; 1 folder refused.", "deep link #workspaces?account=alice: her own policy, focused", alice);
    const rowIds = await page.$$eval("[data-ws-account]", (els) => els.map((e) => e.dataset.wsAccount));
    check(!rowIds.includes("castor") && rowIds.includes("bob") && (await page.textContent("[data-ws-scope='accounts']")).includes("Entities: their folders are set in Manage"), "per-account rows = users; entities say where theirs live", rowIds);
    // bob: Own policy on -> an entry exists; a folder row applies on blur; off -> inline confirm -> gone.
    const bobSw = page.locator("#ws-own-default-bob");
    check((await bobSw.getAttribute("aria-checked")) === "false" && (await page.textContent("[data-ws-account='bob'] .ws-acc__summary")) === "Follows the gateway policy.", "bob follows the gateway policy");
    await bobSw.click();
    await page.waitForSelector("[data-ws-account='bob'] .ws-acc__body [role=radiogroup]", { timeout: 10000 });
    check((await policy("bob")).customized === true, "Own policy on -> PUT /admin/user-workspace-policy (an entry)");
    await page.click("[data-ws-account='bob'] [data-ws-add$='-allowed']");
    const bi = page.locator("[data-ws-account='bob'] [id$='-allowed'] li:last-child input");
    await bi.fill(`${FOLDERS}/notes`);
    await bi.press("Tab");
    await page.waitForFunction(() => document.querySelector("[data-ws-account='bob'] [id$='-allowed'] li:last-child .ws-folder__state").textContent === "Saved", null, { timeout: 10000 });
    check(JSON.stringify((await policy("bob")).policy.workspace_allowed_paths) === JSON.stringify([`${FOLDERS}/notes`]), "bob's folder saved on blur");
    await page.click("[data-ws-account='bob'] [data-ws-mode='blacklist']");
    await page.waitForFunction(() => document.querySelector("[data-ws-account='bob'] [data-ws-mode='blacklist']").getAttribute("aria-checked") === "true", null, { timeout: 10000 });
    const bp = (await policy("bob")).policy;
    check(bp.mode === "blacklist" && JSON.stringify(bp.workspace_allowed_paths) === JSON.stringify([`${FOLDERS}/notes`]), "bob's mode saved; his folder kept (whole entry)", bp);
    await bobSw.click();
    await page.waitForSelector("[data-ws-account='bob'] .ws-confirm", { timeout: 5000 });
    check((await page.textContent("[data-ws-account='bob'] .ws-confirm")).includes("Drop bob's own policy? Their agents follow the gateway policy again."), "turning Own policy off asks inline first");
    check((await policy("bob")).customized === true, "nothing is dropped before the confirmation");
    await page.click("[data-ws-account='bob'] .ws-confirm button.danger");
    await page.waitForFunction(() => !document.querySelector("[data-ws-account='bob'] .ws-acc__body [role=radiogroup]"), null, { timeout: 10000 });
    check((await policy("bob")).customized === false && (await page.textContent("[data-ws-account='bob'] .ws-acc__summary")) === "Follows the gateway policy.", "Drop -> back to the gateway policy");
    // Leaving the page drops its account link.
    await page.evaluate(() => document.getElementById("tab-button-users").click());
    check((await page.evaluate(() => location.hash)) === "", "leaving Workspaces drops #workspaces?account=", await page.evaluate(() => location.hash));
    await ctx.close();
  }
  // ---------------------------------------------------------------- a user (alice): her own policy only
  {
    const { ctx, page } = await open(browser, 1440, 1000, { user: "alice", token: ALICE });
    await gotoWorkspaces(page);
    const view = await page.evaluate(() => ({ gateway: !!document.querySelector("[data-ws-scope='gateway']"), accounts: !!document.querySelector("[data-ws-scope='accounts']"), self: !!document.querySelector("[data-ws-scope='self']"), on: document.getElementById("ws-own-self").getAttribute("aria-checked"), allowed: Array.from(document.querySelectorAll("#ws-self-allowed input")).map((i) => i.value), legacy: !!document.getElementById("ws-self-legacy"), summary: document.querySelector("[data-ws-summary]").textContent }));
    check(!view.gateway && !view.accounts && view.self && view.on === "true" && !view.legacy && JSON.stringify(view.allowed) === JSON.stringify([`${FOLDERS}/alice-lab`]), "a user sees only their own policy (no gateway editor, no account list, no admin grant)", view);
    check(view.summary === "Your own policy: your agents may use only the gateway's allowed folders, 1 folder of yours; 1 folder refused.", "a user's one-line summary", view.summary);
    await page.click("[data-ws-add='ws-self-blocked']");
    const si = page.locator("#ws-self-blocked li:last-child input");
    await si.fill(`${FOLDERS}/secrets`);
    await si.press("Enter");
    await page.waitForFunction(() => document.querySelector("#ws-self-blocked li:last-child .ws-folder__state").textContent === "Saved", null, { timeout: 10000 });
    check((await policy("alice")).policy.workspace_blocked_paths.includes(`${FOLDERS}/secrets`), "a user's row saves through PUT /workspace/policy/self");
    await ctx.close();
  }
  // ---------------------------------------------------------------- Runtimes filter (Accounts Runtime link)
  {
    const { ctx, page } = await open(browser, 1440, 1000);
    await page.evaluate(() => document.getElementById("tab-button-users").click());
    await page.waitForSelector("tr[data-user='alice'] a.accounts-runtime-link");
    const link = await page.evaluate(() => { const a = document.querySelector("tr[data-user='alice'] a.accounts-runtime-link"); return { href: a.getAttribute("href"), text: a.textContent }; });
    check(JSON.stringify(link) === JSON.stringify({ href: "#runtimes?account=alice", text: "alice" }), "Runtime cell = a link to the filtered Runtimes page", link);
    const req = page.waitForRequest((r) => r.url().includes("/api/gateway/admin/runtimes?account=alice"), { timeout: 10000 }).then(() => true, () => false);
    await page.click("tr[data-user='alice'] a.accounts-runtime-link");
    await page.waitForSelector("#tab-runtimes.active #runtimes-filter:not([hidden]) [data-runtimes-filter='alice']", { timeout: 15000 });
    check(await req, "the filter is the server's: GET /admin/runtimes?account=alice");
    const filtered = async () => page.evaluate(() => ({ chip: document.querySelector("#runtimes-filter").hidden ? null : document.querySelector(".runtimes-filter__chip").textContent.trim(), rows: Array.from(document.querySelectorAll("#runtimes-table tr[data-rtkey]")).map((tr) => tr.dataset.rtkey), hash: location.hash }));
    await page.waitForFunction(() => document.querySelectorAll("#runtimes-table tr[data-rtkey]").length > 0, null, { timeout: 15000 });
    let f = await filtered();
    check(f.chip === "Account: alice" && JSON.stringify(f.rows) === JSON.stringify(["user|default|alice"]) && f.hash === "#runtimes?account=alice", "chip names the account; only its runtime is listed", f);
    await page.reload();
    // The session cookie survives the reload; sign in again only if the page asks.
    await page.waitForFunction(() => document.body.classList.contains("signed-in") || (document.getElementById("login-section") && !document.getElementById("login-section").classList.contains("hidden") && document.getElementById("login-user").offsetParent), null, { timeout: 20000 });
    if (!(await page.evaluate(() => document.body.classList.contains("signed-in")))) {
      await page.fill("#login-user", "admin");
      await page.fill("#login-token", ADMIN);
      await page.click("#login-button");
    }
    await page.waitForSelector("#tab-runtimes.active [data-runtimes-filter='alice']", { timeout: 20000 });
    await page.waitForFunction(() => document.querySelectorAll("#runtimes-table tr[data-rtkey]").length > 0, null, { timeout: 15000 });
    f = await filtered();
    check(f.chip === "Account: alice" && f.rows.length === 1, "the deep link survives a reload", f);
    await page.click("[data-runtimes-filter-clear]");
    await page.waitForFunction(() => document.getElementById("runtimes-filter").hidden && document.querySelectorAll("#runtimes-table tr[data-rtkey]").length > 1, null, { timeout: 15000 });
    f = await filtered();
    check(f.chip === null && f.rows.includes("default|default|default") && f.rows.includes("entity|default|runtime_castor") && f.hash === "#runtimes", "× clears the filter: every runtime again", f);
    const ws = await page.evaluate(() => { const tr = document.querySelector("#runtimes-table tr[data-rtkey='user|default|alice']"); const a = tr && tr.querySelector("a.ws-link"); return a && { text: a.textContent, href: a.getAttribute("href") }; });
    check(ws && ws.text === "Own policy" && ws.href === "#workspaces?account=alice", "the Runtimes Workspace cell links to the Workspaces page", ws);
    await ctx.close();
  }
  // ---------------------------------------------------------------- responsive: phone and tablet
  for (const [w, h] of [[834, 1194], [390, 844]]) {
    const { ctx, page } = await open(browser, w, h, { touch: true });
    await gotoWorkspaces(page);
    const m = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, iw: innerWidth, removeH: Math.min(...Array.from(document.querySelectorAll("[data-ws-remove]")).map((b) => b.getBoundingClientRect().height)), inputH: Math.min(...Array.from(document.querySelectorAll(".ws-folder input")).map((i) => i.getBoundingClientRect().height)) }));
    check(m.sw <= m.iw && m.removeH >= 44 && m.inputH >= 40, `${w}: Workspaces fits, 44 px targets`, m);
    await ctx.close();
  }
} catch (e) {
  failures.push(`exception: ${String((e && e.message) || e).split("\n").slice(0, 6).join(" | ")}`);
} finally {
  await browser.close();
}
console.log(JSON.stringify({ failures, checks }));
