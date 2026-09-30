// Browser checks of the web console's state toggles, sign-in card and type scale
// (DESIGN 2026-09-30 §2–§6), run by tests/test_gateway_console_browser_state_toggles.py
// against a hermetic scratch gateway (admin / alice with an email address and a mailbox record
// that never connects / bob without an email address).
//
//   node state_toggles.mjs <base-url> <admin-token> <alice-token> <playwright-node-modules> [<kit ui-kit dir>]
//
// Prints one JSON line: {"failures": [...], "checks": N}. The type-scale check is the kit's own
// checkLabelScale (ui-kit src/label_scale.ts, types stripped by node) when the kit checkout is
// present, else the same rule written out here.
import { createRequire } from "node:module";
import module from "node:module";
import fs from "node:fs";
import path from "node:path";

const [BASE, ADMIN, ALICE, PW, KIT] = process.argv.slice(2);
const require = createRequire(path.join(PW, "/"));
const { chromium } = require("playwright-core");

let labelScaleSource = `
  window.checkLabelScale = function (root) {
    const sel = "label, .af-switch__label, .af-form__label, .af-gateway-signin__label, .af-gateway-signin__checkbox, .af-field-caption, [data-af-caption]";
    const out = [];
    for (const el of root.querySelectorAll(sel)) {
      if (el.getClientRects().length === 0) continue;
      const cs = getComputedStyle(el);
      const size = parseFloat(cs.fontSize) || 0;
      const w = cs.fontWeight === "bold" ? 700 : (parseFloat(cs.fontWeight) || 400);
      if (size > 15.01 || w > 600) out.push({ selector: el.tagName.toLowerCase() + "." + String(el.className || "").trim().split(/\\s+/).join("."), text: (el.textContent || "").trim().slice(0, 80), fontSize: size, fontWeight: w });
    }
    return out;
  };`;
let labelScaleOrigin = "inline";
if (KIT && fs.existsSync(path.join(KIT, "src", "label_scale.ts"))) {
  const ts = fs.readFileSync(path.join(KIT, "src", "label_scale.ts"), "utf8");
  const js = module.stripTypeScriptTypes(ts).replace(/^export\s+/gm, "");
  labelScaleSource = `(function(){ ${js}\n window.checkLabelScale = (root) => checkLabelScale(root).map((h) => ({ selector: h.selector, text: h.text, fontSize: h.fontSize, fontWeight: h.fontWeight })); })();`;
  labelScaleOrigin = "kit";
}

const failures = [];
let checks = 0;
function check(ok, what, detail) {
  checks += 1;
  if (!ok) failures.push(detail === undefined ? what : `${what}: ${JSON.stringify(detail)}`);
}

async function newPage(browser, viewport = { width: 1440, height: 900 }) {
  const ctx = await browser.newContext({ viewport });
  await ctx.addInitScript(labelScaleSource);
  const page = await ctx.newPage();
  page.on("pageerror", (e) => failures.push(`pageerror: ${e.message}`));
  return { ctx, page };
}

async function signIn(page, user, token) {
  await page.goto(`${BASE}/console`);
  await page.waitForSelector("#login-form");
  await page.fill("#login-user", user);
  await page.fill("#login-token", token);
  await page.click("#login-button");
  await page.waitForFunction(() => document.body.classList.contains("signed-in"), null, { timeout: 30000 });
  await page.keyboard.press("Escape").catch(() => {});
}

async function openAccount(page) {
  await page.evaluate(() => document.getElementById("tab-button-users").click());
  await page.waitForSelector("#tab-users.active");
  await page.waitForFunction(() => { const s = document.getElementById("my-email-notify-job-failed"); return s && document.getElementById("my-email-registered") && window.__af_loaded !== false; });
  await page.waitForTimeout(1200);
}

async function labelScale(page, selector, what) {
  const hits = await page.evaluate((sel) => { const root = document.querySelector(sel); return root ? window.checkLabelScale(root) : [{ selector: "missing root " + sel }]; }, selector);
  check(hits.length === 0, `type scale ${what}`, hits);
}

const browser = await chromium.launch();
try {
  // ---------------------------------------------------------------- sign-in card + recovery
  {
    const { ctx, page } = await newPage(browser);
    let release;
    const held = new Promise((r) => { release = r; });
    await page.route("**/api/gateway/session/recovery", (r) => r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ available: true, purposes: ["sign_in", "reset_token"] }) }));
    await page.route("**/api/gateway/session/recovery/request", async (r) => {
      await held;
      await r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ sent: true, to: "a•••@•••", expires_in_s: 600, message: "A sign-in code is on its way to a•••@•••. It expires in 10 minutes." }) });
    });
    await page.goto(`${BASE}/console`);
    await page.waitForSelector("#login-form");
    await page.waitForSelector("#recovery-link", { state: "visible" });
    await labelScale(page, "#login-section", "sign-in card");
    const pills = await page.locator("#login-section .af-gateway-signin__status").count();
    check(pills === 1, "one status pill", pills);
    check((await page.textContent("#login-status")).trim() === "Not signed in", "pill says Not signed in");
    check(await page.locator("#recovery-code-step").isHidden(), "code step hidden before the link");
    await page.fill("#login-user", "alice");
    await page.click("#recovery-link");
    await page.waitForTimeout(200);
    check((await page.textContent("#recovery-link")).trim() === "Sending…", "link turns into Sending…");
    check((await page.getAttribute("#recovery-link", "aria-busy")) === "true", "link is aria-busy");
    release();
    await page.waitForSelector("#recovery-code-step", { state: "visible", timeout: 5000 });
    check(await page.locator("#recovery-section").isHidden(), "the link is replaced by the code step");
    check((await page.textContent("#recovery-sent-message")).includes("It expires in 10 minutes."), "honest sent message");
    check(await page.evaluate(() => document.activeElement && document.activeElement.id === "recovery-code-input"), "code field focused");
    check(await page.isDisabled("#recovery-use"), "Use code disabled before 8 digits");
    check(/^Send a new code \(in \d+ s\)$/.test((await page.textContent("#recovery-resend")).trim()), "resend cooldown text", await page.textContent("#recovery-resend"));
    await page.fill("#recovery-code-input", "12345678");
    check(!(await page.isDisabled("#recovery-use")), "Use code enabled at 8 digits");
    await page.click("#recovery-back");
    check(await page.locator("#recovery-code-step").isHidden() && await page.locator("#recovery-link").isVisible(), "Back to token restores the link");
    // A refused token: inline under the Token field, pill "Token refused".
    await page.fill("#login-user", "admin");
    await page.fill("#login-token", "not-the-token-000000000");
    await page.click("#login-button");
    await page.waitForSelector("#login-token-error", { state: "visible", timeout: 10000 });
    check((await page.textContent("#login-token-error")).trim() === "This token was refused.", "refused token inline");
    check((await page.textContent("#login-status")).trim() === "Token refused", "pill says Token refused");
    await ctx.close();
  }
  // ---------------------------------------------------------------- admin: users + switch + create user + account page
  {
    const { ctx, page } = await newPage(browser);
    await signIn(page, "admin", ADMIN);
    await openAccount(page);
    await page.waitForSelector("#users-table tr[data-user='bob']");
    await page.evaluate(() => { document.getElementById("email-caps-advanced").open = true; });
    await page.waitForFunction(() => document.getElementById("email-cap-email").getAttribute("aria-checked") === "true", null, { timeout: 10000 });
    await labelScale(page, "#users-section", "users section");
    const caps = await page.evaluate(() => ["email-cap-email", "email-cap-agent-tools", "email-cap-recovery"].map((id) => { const b = document.getElementById(id); return [id, b.getAttribute("role"), b.getAttribute("aria-checked")]; }));
    check(JSON.stringify(caps) === JSON.stringify([["email-cap-email", "switch", "true"], ["email-cap-agent-tools", "switch", "true"], ["email-cap-recovery", "switch", "true"]]), "admin switches on by default", caps);
    const cols = await page.$$eval("#users-section thead th", (ths) => ths.map((t) => t.textContent.trim()));
    check(JSON.stringify(cols) === JSON.stringify(["User", "Role", "Email address", "Mailbox", "Runtime", "Active", "Actions"]), "users columns", cols);
    const own = await page.evaluate(() => { const b = document.querySelector("tr[data-user='admin'] .users-active [role=switch]"); const r = b && document.getElementById(b.id + "-reason"); return b && { dis: b.getAttribute("aria-disabled"), reason: r && !r.hidden ? r.textContent : null }; });
    check(own && own.dis === "true" && own.reason === "You can't deactivate your own account.", "own Active row unavailable with the reason", own);
    const alice = await page.textContent("tr[data-user='alice'] .users-mailbox");
    check(alice.includes("connected as alice@fastmail.com"), "alice mailbox cell", alice);
    const bobEmail = await page.textContent("tr[data-user='bob'] td[data-label='Email address']");
    check(bobEmail.trim() === "—", "bob has no email address", bobEmail);
    const bobCell = (await page.textContent("tr[data-user='bob'] .users-mailbox")).trim();
    check(bobCell.includes("agent email tools not allowed for this user") && (await page.locator("tr[data-user='bob'] .users-mailbox button").count()) === 1, "old per-user override shown with Reset", bobCell);
    check(!(await page.textContent("tr[data-user='alice'] .users-mailbox")).includes("not allowed"), "no note without an override");
    await page.click("tr[data-user='bob'] .users-active [role=switch]");
    await page.waitForSelector("#users-table .row-confirm");
    const confirmText = await page.textContent("#users-table .row-confirm");
    check(confirmText.includes("Deactivate bob? They are signed out until you turn Active back on."), "inline deactivate confirmation", confirmText);
    check((await page.getAttribute("tr[data-user='bob'] .users-active [role=switch]", "aria-checked")) === "true", "Active stays on until confirmed");
    await page.click("#users-table .row-confirm button.secondary");
    check((await page.locator("#users-table .row-confirm").count()) === 0, "Cancel closes the confirmation");
    // Create user modal.
    await page.click("#open-create-user");
    const adv = await page.evaluate(() => { const d = document.querySelector("#user-create-form details"); const r = document.getElementById("new-runtime"); return { open: d.open, runtimeShown: r.checkVisibility() }; });
    check(adv.open === false && adv.runtimeShown === false, "Create user Advanced collapsed by default", adv);
    await page.evaluate(() => { for (const d of document.querySelectorAll("#user-create-form details")) d.open = true; });
    await labelScale(page, "#user-create-form", "create user modal");
    const emailTop = await page.evaluate(() => { const i = document.getElementById("new-email"); return !i.closest("details"); });
    check(emailTop, "Email address at the top level of Create user");
    await page.click("#create-user-cancel");
    // The admin's own account page (no mailbox yet).
    await page.evaluate(() => { document.getElementById("my-email-advanced").open = true; document.getElementById("my-email-servers").open = true; });
    await page.click("#my-email-tab-other");
    await labelScale(page, "#my-email-section", "account page");
    const nf = await page.evaluate(() => ["my-email-notify-job-failed", "my-email-notify-approval", "my-email-agent-tools"].map((id) => { const b = document.getElementById(id); const r = document.getElementById(id + "-reason"); return [b.getAttribute("aria-disabled"), r.hidden ? "" : r.textContent]; }));
    check(nf.every(([d, r]) => d === "true" && r === "Connect a mailbox first."), "switches unavailable until a mailbox is connected", nf);
    const widths = await page.$$eval("#my-email-section .af-form", (fs) => fs.filter((f) => f.getClientRects().length).map((f) => Math.round(f.getBoundingClientRect().width)));
    check(widths.length > 0 && widths.every((w) => w <= 720), "forms at most 720 px", widths);
    const grid = await page.evaluate(() => { const g = document.querySelector("#my-email-servers .af-form__grid-2"); return getComputedStyle(g).gridTemplateColumns.split(" ").length; });
    check(grid === 2, "port + security side by side at 1440 px", grid);
    await ctx.close();
  }
  // ---------------------------------------------------------------- phone: one column, label scale
  {
    const { ctx, page } = await newPage(browser, { width: 390, height: 844 });
    await signIn(page, "admin", ADMIN);
    await openAccount(page);
    await page.evaluate(() => { document.getElementById("my-email-servers").open = true; });
    await page.click("#my-email-tab-other");
    const grid = await page.evaluate(() => { const g = document.querySelector("#my-email-servers .af-form__grid-2"); return getComputedStyle(g).gridTemplateColumns.split(" ").length; });
    check(grid === 1, "short fields stack at 390 px", grid);
    await page.waitForSelector("tr[data-user='bob'] .users-mailbox__body");
    const cell = await page.evaluate(() => {
      const body = document.querySelector("tr[data-user='bob'] .users-mailbox__body");
      const kids = Array.from(body.children).map((k) => k.getBoundingClientRect());
      const status = body.querySelector(".users-mailbox__status");
      const r = document.createRange(); r.selectNodeContents(status);
      return { tops: kids.map((k) => Math.round(k.top)), lefts: kids.map((k) => Math.round(k.left)), statusLines: r.getClientRects().length, resetH: Math.round(kids[kids.length - 1].height) };
    });
    const stacked = cell.tops.every((t, i) => i === 0 || t > cell.tops[i - 1]) && new Set(cell.lefts).size === 1;
    check(stacked && cell.statusLines === 1 && cell.resetH >= 44, "phone Mailbox cell stacks status / note / Reset, no word split", cell);
    await page.click("tr[data-user='bob'] .users-mailbox button");
    await page.waitForFunction(() => !document.querySelector("tr[data-user='bob'] .users-mailbox").textContent.includes("not allowed"), null, { timeout: 10000 });
    check(true, "Reset clears the override");
    await labelScale(page, "#my-email-section", "account page (phone)");
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
    check(!overflow, "no horizontal page scroll at 390 px");
    await ctx.close();
  }
  // ---------------------------------------------------------------- alice: connected mailbox
  {
    const { ctx, page } = await newPage(browser);
    await signIn(page, "alice", ALICE);
    await openAccount(page);
    await page.waitForSelector("#my-email-connected", { state: "visible", timeout: 10000 });
    const status = (await page.textContent("#my-email-status")).trim();
    check(status.startsWith("Connected as alice@fastmail.com · password"), "connected status line", status);
    check(await page.locator("#my-email-connect").isHidden(), "no form fields in the connected view");
    const nf = await page.evaluate(() => ["my-email-notify-job-failed", "my-email-notify-approval"].map((id) => [document.getElementById(id).getAttribute("aria-disabled"), document.getElementById(id).getAttribute("aria-checked")]));
    check(nf.every(([d, c]) => d === null && c === "true"), "notification switches available and on by default", nf);
    check((await page.inputValue("#my-email-registered")) === "alice@fastmail.com", "email address field");
    await page.evaluate(() => { document.getElementById("my-email-advanced").open = true; });
    await page.fill("#my-email-imap-folder", "Archive");
    await page.locator("#my-email-imap-folder").blur();
    await page.waitForFunction(() => document.getElementById("my-email-folder-state").textContent.trim() !== "", null, { timeout: 10000 });
    check((await page.textContent("#my-email-folder-state")).trim() === "Saved", "Folder auto-saves", await page.textContent("#my-email-folder-state"));
    await page.click("#my-email-disconnect");
    check((await page.textContent("#my-email-disconnect-confirm")).includes("Disconnect this mailbox?"), "inline disconnect confirmation");
    await page.click("#my-email-disconnect-cancel");
    await ctx.close();
  }
} finally {
  await browser.close();
}
console.log(JSON.stringify({ failures, checks, labelScale: labelScaleOrigin }));
