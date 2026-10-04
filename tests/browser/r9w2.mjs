// Browser checks of the round-9 console (DESIGN.md R9.2), run by
// tests/test_gateway_console_browser_r9w2.py against a hermetic scratch gateway (admin, alice, bob,
// the entity castor; the gateway allows <folders>/projects and never <folders>/secrets).
//
//   node r9w2.mjs <base-url> <admin-token> <playwright-node-modules> <folders-dir> <alice-token>
//
// Prints one JSON line: {"failures": [...], "checks": N}.
import { createRequire } from "node:module";
import path from "node:path";

const [BASE, ADMIN, PW, FOLDERS, ALICE] = process.argv.slice(2);
const require = createRequire(path.join(PW, "/"));
const { chromium } = require("playwright-core");
const F = (name) => path.join(FOLDERS, name);

const failures = [];
let checks = 0;
function check(ok, what, detail) {
  checks += 1;
  if (!ok) failures.push(detail === undefined ? what : `${what}: ${JSON.stringify(detail)}`);
}
async function apiAs(token, method, p, body) {
  const r = await fetch(`${BASE}/api/gateway${p}`, { method, headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });
  return { status: r.status, body: await r.json().catch(() => ({})) };
}
const gw = async () => (await apiAs(ADMIN, "GET", "/workspace/policy")).body.policy;
const acct = async (key) => (await apiAs(ADMIN, "GET", `/workspace/policy/${key}`)).body;

async function open(browser, { width = 1440, height = 900, user = "admin", token = ADMIN, hash = "" } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => { errors.push(e.message); failures.push(`pageerror@${width}/${user}: ${e.message}`); });
  page.on("console", (m) => { if (m.type() === "error" && !/Failed to load resource/.test(m.text())) errors.push(m.text()); });
  await page.goto(`${BASE}/console${hash}`);
  await page.waitForSelector("#login-form");
  await page.fill("#login-user", user);
  await page.fill("#login-token", token);
  await page.click("#login-button");
  await page.waitForFunction(() => document.body.classList.contains("signed-in"), null, { timeout: 20000 });
  await page.keyboard.press("Escape").catch(() => {});
  await page.waitForSelector("#users-table tr.accounts-row", { timeout: 15000 });
  await page.waitForTimeout(300);
  return { ctx, page, errors };
}
// Blur-apply a value into a folder input (Enter blurs), then wait for its row state.
async function typeAndBlur(page, input, value) {
  await input.click();
  await input.fill(value);
  await input.press("Enter");
}

const browser = await chromium.launch();
try {
  // ------------------------------------------------------------------ W1: no Workspaces page
  {
    const { ctx, page, errors } = await open(browser, { hash: "#workspaces?account=alice" });
    check(await page.evaluate(() => !document.getElementById("tab-button-workspaces") && !document.getElementById("tab-workspaces")), "no Workspaces sidebar entry or page");
    check(await page.evaluate(() => document.getElementById("tab-users").classList.contains("active")), "#workspaces?account= lands on Accounts");
    check(errors.length === 0, "no console error on the old deep link", errors);
    await ctx.close();
  }
  // ------------------------------------------------------------------ W2: the gateway policy modal
  {
    const { ctx, page } = await open(browser);
    const btn = page.locator("#accounts-gw-workspace");
    check(await btn.isVisible() && (await btn.textContent()) === "Shared workspace & allowed folders", "admin sees the gateway button");
    const first = await page.evaluate(() => document.querySelector(".accounts-head__actions > :not([hidden]):not(.hidden)").id);
    check(first === "accounts-gw-workspace", "the button is the first control at the top of Accounts", first);
    await btn.click();
    await page.waitForSelector("#gateway-workspace-backdrop:not([hidden]) [data-ws-summary]");
    let p = await gw();
    const line = async () => page.textContent("[data-ws-summary]");
    check((await line()) === `Only allowed folders · Shared workspace (rw) · ${F("projects")} (rw)`, "the effective line, byte-exact", await line());
    check((await page.inputValue("#wsg-shared")) === p.shared_workspace && (await page.textContent("[data-ws-shared-mode]")) === "Read & write", "the shared workspace: the stored folder, Read & write");
    check((await page.locator("#gateway-workspace-body button:text-is('Save')").count()) === 0, "no Save button");
    // Two dimensions only: the posture segmented switch and the per-row permission; no switch at all.
    check((await page.locator("#gateway-workspace-body [role=switch]").count()) === 0, "no switch in the gateway modal (no launch-folder trust, no allow-any)");
    const postures = await page.evaluate(() => Array.from(document.querySelectorAll("[role=radiogroup] [data-ws-posture]")).map((b) => [b.querySelector(".ui-seg__title").textContent, b.getAttribute("aria-checked")]));
    check(JSON.stringify(postures) === JSON.stringify([["Only allowed folders", "true"], ["Any folder except denied", "false"]]), "two postures, Only allowed folders on", postures);
    check((await page.locator("#wsg-default").count()) === 0, "no default mode under Only allowed folders");
    // Shared workspace: invalid, empty, then valid.
    const shared = page.locator("#wsg-shared");
    await typeAndBlur(page, shared, F("a-file.txt"));
    await page.waitForFunction(() => /Not saved\.$/.test(document.querySelector("#wsg-shared").closest(".ws-folder").querySelector(".ws-folder__state").textContent));
    check((await gw()).shared_workspace === p.shared_workspace, "an invalid shared workspace is not saved");
    await typeAndBlur(page, shared, "");
    check((await page.textContent("#wsg-shared ~ .ws-folder__state")) === "The shared workspace is required. Not saved." && (await shared.inputValue()) === p.shared_workspace, "the shared workspace cannot be emptied");
    await typeAndBlur(page, shared, F("shared"));
    await page.waitForFunction(() => document.querySelector("#wsg-shared ~ .ws-folder__state").textContent === "Saved");
    check((await gw()).shared_workspace === F("shared"), "a valid shared workspace applies on blur", (await gw()).shared_workspace);
    // A row: added Read & write, then lowered to Read-only (granular).
    await page.click("[data-ws-add='wsg-folders']");
    check((await gw()).folders.length === 1, "adding an empty row writes nothing");
    await typeAndBlur(page, page.locator("#wsg-folders li:last-child input"), F("notes"));
    await page.waitForFunction(() => document.querySelector("#wsg-folders li:last-child .ws-folder__state").textContent === "Saved");
    p = await gw();
    check(JSON.stringify(p.folders) === JSON.stringify([{ path: F("projects"), mode: "rw" }, { path: F("notes"), mode: "rw" }]), "a folder applies on blur, Read & write by default", p.folders);
    await page.click("#wsg-folders li:last-child [data-ws-mode='ro']");
    await page.waitForFunction(() => document.querySelector("#wsg-folders li:last-child .ws-folder__state").textContent === "Saved");
    p = await gw();
    check(JSON.stringify(p.folders) === JSON.stringify([{ path: F("projects"), mode: "rw" }, { path: F("notes"), mode: "ro" }]), "one folder Read & write, one Read-only (granular)", p.folders);
    check((await line()) === `Only allowed folders · Shared workspace (rw) · ${F("projects")} (rw) · ${F("notes")} (ro)`, "the line follows the modes", await line());
    await page.click("[data-ws-add='wsg-folders']");
    await typeAndBlur(page, page.locator("#wsg-folders li:last-child input"), F("nope-missing"));
    await page.waitForFunction(() => /Not saved\.$/.test(document.querySelector("#wsg-folders li:last-child .ws-folder__state").textContent));
    check((await gw()).folders.length === 2, "a missing folder is refused with its sentence and not saved");
    await page.click("#wsg-folders li:last-child [data-ws-remove]");
    // Posture (b): Any folder except denied, ONE default mode, rows are exceptions (a new row starts Denied).
    await page.click("[data-ws-posture='any_except_denied']");
    await page.waitForSelector("#wsg-default");
    check((await gw()).posture === "any_except_denied", "the posture applies at once");
    check((await line()).startsWith("Any folder except denied (rw) · Shared workspace (rw)"), "the line under Any folder except denied", await line());
    await page.click("#wsg-default [data-ws-default='ro']");
    await page.waitForTimeout(500);
    check((await gw()).default_mode === "ro" && (await line()).startsWith("Any folder except denied (ro) · "), "the default mode for everything else applies at once", (await gw()).default_mode);
    await page.click("[data-ws-add='wsg-folders']");
    await typeAndBlur(page, page.locator("#wsg-folders li:last-child input"), F("secrets"));
    await page.waitForFunction(() => document.querySelector("#wsg-folders li:last-child .ws-folder__state").textContent === "Saved");
    check((await gw()).folders.some((r) => r.path === F("secrets") && r.mode === "deny") && (await line()).includes(`${F("secrets")} (denied)`), "an exception row starts Denied", (await gw()).folders);
    // Reopen: the stored values.
    await page.keyboard.press("Escape");
    await page.waitForSelector("#gateway-workspace-backdrop[hidden]", { state: "attached" });
    await btn.click();
    await page.waitForSelector("#gateway-workspace-backdrop:not([hidden]) [data-ws-summary]");
    const shown = await page.evaluate(() => ({ shared: document.querySelector("#wsg-shared").value, rows: Array.from(document.querySelectorAll("#wsg-folders li")).map((li) => [li.querySelector("input").value, li.querySelector("[data-ws-mode][aria-checked='true']").dataset.wsMode]), posture: document.querySelector("[data-ws-posture][aria-checked='true']").dataset.wsPosture, def: document.querySelector("#wsg-default [aria-checked='true']").dataset.wsDefault }));
    check(shown.shared === F("shared") && shown.posture === "any_except_denied" && shown.def === "ro" && JSON.stringify(shown.rows) === JSON.stringify([[F("projects"), "rw"], [F("notes"), "ro"], [F("secrets"), "deny"]]), "a reopen shows the stored values", shown);
    await page.click("#gateway-workspace-close");
    // Back to the seeded posture for the account checks.
    await apiAs(ADMIN, "PUT", "/workspace/policy", { posture: "allowed_only", default_mode: "rw", folders: [{ path: F("projects"), mode: "rw" }, { path: F("notes"), mode: "ro" }] });
    await ctx.close();
  }
  // ------------------------------------------------------------------ W3: per-account modals
  {
    const { ctx, page } = await open(browser);
    const rowsWithFolder = await page.evaluate(() => Array.from(document.querySelectorAll("#users-table tr.accounts-row")).filter((tr) => tr.querySelector("button[data-action='workspace']")).map((tr) => tr.dataset.user).sort());
    check(JSON.stringify(rowsWithFolder) === JSON.stringify(["admin", "alice", "bob", "castor"]), "the folder icon on every live row (users, entity, admin)", rowsWithFolder);
    await page.click("tr[data-user='alice'] button[data-action='workspace']");
    await page.waitForSelector("#account-workspace-body [data-workspace='effective']");
    check((await page.textContent("#account-workspace-title")) === "Workspace folders — alice", "the modal names the account");
    const before = await acct("default:alice");
    check((await page.textContent("[data-workspace='effective']")) === `Agents may use: ${before.effective.summary}`, "the effective set in one line, from the gateway", [await page.textContent("[data-workspace='effective']"), before.effective.summary]);
    check((await page.textContent("[data-workspace='shared-always']")) === "Always on" && (await page.textContent("#account-workspace-body")).includes(F("shared")), "the shared workspace is shown, always on");
    const extras = await page.evaluate(() => Array.from(document.querySelectorAll("[data-workspace='extra']")).map((r) => [r.dataset.path, r.querySelector("[role=switch]").getAttribute("aria-checked")]));
    check(JSON.stringify(extras.map((e) => e[0])) === JSON.stringify([F("projects"), F("notes")]) && extras.every((e) => e[1] === "false"), "one switch per allowed folder, off by default", extras);
    await page.click(`[data-workspace='extra'][data-path='${F("notes")}'] [role=switch]`);
    await page.waitForSelector(`[data-workspace='extra'][data-path='${F("notes")}'] [data-workspace='saved']`);
    check(JSON.stringify((await acct("default:alice")).policy.enabled_folders) === JSON.stringify([F("notes")]), "a switch is one PUT for that account");
    // My folders visible (posture Any folder except denied): add one, then refuse a Never allowed one.
    check((await page.locator("[data-workspace='own-add']").count()) === 1, "My folders rows while the posture is Any folder except denied");
    await page.fill("[data-workspace='own-add'] input", F("alice-lab"));
    await page.press("[data-workspace='own-add'] input", "Enter");
    await page.waitForSelector(`[data-workspace='own'][data-path='${F("alice-lab")}']`);
    check(JSON.stringify((await acct("default:alice")).policy.own_folders) === JSON.stringify([F("alice-lab")]), "an own folder is added");
    await page.fill("[data-workspace='own-add'] input", F("secrets"));
    await page.press("[data-workspace='own-add'] input", "Enter");
    await page.waitForSelector("[data-workspace='own-add'] [data-workspace='refusal']");
    check((await page.textContent("[data-workspace='own-add'] [data-workspace='refusal']")).endsWith("Not saved.") && !(await acct("default:alice")).policy.own_folders.includes(F("secrets")), "a Never allowed own folder is refused inline", await page.textContent("[data-workspace='own-add'] [data-workspace='refusal']"));
    // Follow the gateway policy: inline confirm, then everything off.
    await page.click("[data-ws-reset]");
    check((await page.textContent(".wsm-confirm")).includes("Follow the gateway policy for alice?"), "Follow the gateway policy asks inline");
    await page.click(".wsm-confirm button.danger");
    await page.waitForSelector(".wsm-reset .ws-folder__state.is-ok");
    const after = await acct("default:alice");
    check(after.policy.enabled_folders.length === 0 && after.policy.own_folders.length === 0, "Follow the gateway policy resets the account", after.policy);
    check(await page.evaluate(() => Array.from(document.querySelectorAll("[data-workspace='extra'] [role=switch]")).every((s) => s.getAttribute("aria-checked") === "false")), "the switches show the reset");
    await page.keyboard.press("Escape");
    await page.waitForSelector("#account-workspace-backdrop[hidden]", { state: "attached" });
    // Entity and the admin's own row open and load.
    for (const who of ["castor", "admin"]) {
      await page.click(`tr[data-user='${who}'] button[data-action='workspace']`);
      await page.waitForSelector("#account-workspace-body [data-workspace='effective'], #account-workspace-body [data-workspace='load-error']");
      const err = await page.locator("#account-workspace-body [data-workspace='load-error']").count();
      check(err === 0, `${who}'s modal loads`, err ? await page.textContent("[data-workspace='load-error']") : "");
      await page.click("#account-workspace-close");
      await page.waitForSelector("#account-workspace-backdrop[hidden]", { state: "attached" });
    }
    // Allow any folder off -> My folders hidden with the sentence.
    await apiAs(ADMIN, "PUT", "/workspace/policy", { posture: "allowed_only" });
    await page.click("tr[data-user='bob'] button[data-action='workspace']");
    await page.waitForSelector("#account-workspace-body [data-workspace='effective']");
    check((await page.locator("[data-workspace='own-add']").count()) === 0 && (await page.textContent("[data-workspace='own-hidden']")) === "My folders appear when the gateway admin allows any folder.", "My folders hidden under Only allowed folders, with the reason");
    await page.click("#account-workspace-close");
    await ctx.close();
  }
  // ------------------------------------------------------------------ non-admin: own modal, no gateway button
  {
    const { ctx, page } = await open(browser, { user: "alice", token: ALICE });
    check(!(await page.locator("#accounts-gw-workspace").isVisible()), "a user does not see the gateway button");
    await page.click("tr[data-user='alice'] button[data-action='workspace']");
    await page.waitForSelector("#account-workspace-body [data-workspace='effective'], #account-workspace-body [data-workspace='load-error']");
    check((await page.locator("[data-workspace='load-error']").count()) === 0, "a user opens their own folders (me)");
    await page.click(`[data-workspace='extra'][data-path='${F("projects")}'] [role=switch]`);
    await page.waitForSelector(`[data-workspace='extra'][data-path='${F("projects")}'] [data-workspace='saved']`);
    check((await acct("default:alice")).policy.enabled_folders.includes(F("projects")), "a user turns an allowed folder on for themselves");
    await page.click("#account-workspace-close");
    await ctx.close();
  }
  // ------------------------------------------------------------------ W4: the kit tooltip
  {
    const { ctx, page } = await open(browser);
    const tips = await page.evaluate(() => Object.fromEntries(Array.from(document.querySelectorAll("tr[data-user='alice'] .accounts-actions__buttons > button, tr[data-user='castor'] .accounts-actions__buttons > button")).map((b) => [`${b.closest("tr").dataset.user}:${b.dataset.action}`, { tip: b.dataset.afTip, title: b.getAttribute("title") }])));
    const want = {
      "alice:email": "Email address and mailbox of alice", "alice:openai_api": "OpenAI API access for alice", "alice:logs": "Activity log of alice",
      "alice:workspace": "Workspace folders alice's agents may use", "alice:rotate": "Rotate alice's sign-in token", "alice:archive": "Archive alice (kept, hidden)",
      "castor:manage": "Manage castor (mind, voice, prompt…)",
    };
    for (const [k, v] of Object.entries(want)) check(tips[k] && tips[k].tip === v && tips[k].title === null, `tooltip ${k}`, tips[k]);
    const sel = "tr[data-user='alice'] button[data-action='archive']";
    await page.hover(sel);
    await page.waitForTimeout(90);
    const early = await page.evaluate(() => { const t = document.querySelector(".af-tooltip"); return !!t && !t.hidden; });
    await page.waitForTimeout(160);
    const late = await page.evaluate(() => { const t = document.querySelector(".af-tooltip"); return t && !t.hidden ? t.textContent : null; });
    check(!early && late === "Archive alice (kept, hidden)", "150 ms delay: nothing at 90 ms, the sentence at 250 ms", { early, late });
    const look = await page.evaluate(() => { const t = document.querySelector(".af-tooltip"); const cs = getComputedStyle(t); const r = t.getBoundingClientRect(); return { pos: cs.position, bg: cs.backgroundColor, fg: cs.color, inside: r.left >= 0 && r.right <= innerWidth && r.top >= 0 && r.bottom <= innerHeight }; });
    check(look.pos === "fixed" && look.bg !== look.fg && look.inside, "themed, fixed, inside the viewport", look);
    await page.keyboard.press("Escape");
    check(await page.evaluate(() => document.querySelector(".af-tooltip").hidden), "Escape hides the tooltip");
    await page.mouse.move(5, 5);
    await page.focus("tr[data-user='alice'] button[data-action='logs']");
    await page.keyboard.press("Tab");
    await page.waitForTimeout(250);
    const kb = await page.evaluate(() => { const t = document.querySelector(".af-tooltip"); return t && !t.hidden ? t.textContent : null; });
    check(kb === "Workspace folders alice's agents may use", "keyboard focus shows the tooltip", kb);
    await page.keyboard.press("Tab");
    await page.waitForTimeout(50);
    const moved = await page.evaluate(() => document.querySelector(".af-tooltip").hidden || document.querySelector(".af-tooltip").textContent !== "Workspace folders alice's agents may use");
    check(moved, "blur hides the previous tooltip");
    // One tooltip element only (the islands binding is shared with the top bar).
    check((await page.locator(".af-tooltip").count()) === 1, "one tooltip element per page", await page.locator(".af-tooltip").count());
    // Top bar island buttons use the same tooltip, no native title.
    const top = await page.evaluate(() => Array.from(document.querySelectorAll("#af-topbar-root .af-topbar__btn")).map((b) => ({ tip: b.dataset.afTip || null, title: b.getAttribute("title") })));
    check(top.length > 0 && top.every((b) => b.tip && b.title === null), "top bar icon buttons use the kit tooltip", top);
    await ctx.close();
  }
  // ------------------------------------------------------------------ phone width: the tooltip stays inside
  {
    const { ctx, page } = await open(browser, { width: 390, height: 844 });
    const sel = "tr[data-user='alice'] button[data-action='archive']";
    await page.locator(sel).scrollIntoViewIfNeeded();
    await page.hover(sel);
    await page.waitForTimeout(300);
    const m = await page.evaluate(() => { const t = document.querySelector(".af-tooltip"); const r = t.getBoundingClientRect(); return { shown: !t.hidden, left: r.left, right: r.right, vw: innerWidth, sw: document.documentElement.scrollWidth }; });
    check(m.shown && m.left >= 8 && m.right <= m.vw - 8 && m.sw <= m.vw, "390 px: the tooltip is inside the viewport and the page does not widen", m);
    check((await page.locator("tr[data-user='castor'] button[data-action='workspace']").count()) === 1, "390 px cards keep the folder icon");
    await ctx.close();
  }
} catch (e) {
  failures.push(`exception: ${e && e.stack ? e.stack : e}`);
} finally {
  await browser.close();
}
console.log(JSON.stringify({ failures, checks }));
