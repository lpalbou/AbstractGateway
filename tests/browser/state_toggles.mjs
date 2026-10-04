// Browser checks of the web console's state toggles, sign-in card and type scale
// (DESIGN 2026-09-30 §2–§6), run by tests/test_gateway_console_browser_state_toggles.py
// against a hermetic scratch gateway (admin / alice with an email address and a mailbox record
// that never connects / bob without an email address).
//
//   node state_toggles.mjs <base-url> <admin-token> <alice-token> <playwright-node-modules> <kit ui-kit dir> <run id> <bob-token>
//
// Prints one JSON line: {"failures": [...], "checks": N}. The type-scale check is the kit's own
// checkLabelScale (ui-kit src/label_scale.ts, types stripped by node) when the kit checkout is
// present, else the same rule written out here.
import { createRequire } from "node:module";
import module from "node:module";
import fs from "node:fs";
import path from "node:path";

const [BASE, ADMIN, ALICE, PW, KIT, RUN_ID, BOB] = process.argv.slice(2);
if (!BOB) throw new Error("state_toggles.mjs needs bob's token (7th argument)");
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

async function newPage(browser, viewport = { width: 1440, height: 900 }, touch = false) {
  const ctx = await browser.newContext(touch ? { viewport, hasTouch: true, isMobile: true } : { viewport });
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

// Touch lower bound (round-1 leftover, DESIGN-v2 §0 item 11): on a touch screen every visible
// reading text (an element whose own text is >= 20 characters; code, inputs and the kit's
// small caps excluded) computes to >= 14 px.
async function fontFloor(page, selector, what) {
  const hits = await page.evaluate((sel) => {
    const root = document.querySelector(sel);
    if (!root) return [{ missing: sel }];
    const out = [];
    for (const el of root.querySelectorAll("*")) {
      if (el.closest("code, pre, input, select, textarea, .af-kind-chip, .af-nav-group__caption, .help-q > summary, svg")) continue;
      if (el.getClientRects().length === 0) continue;
      const own = Array.from(el.childNodes).filter((n) => n.nodeType === 3).map((n) => n.textContent).join("").trim();
      if (own.length < 20) continue;
      const size = parseFloat(getComputedStyle(el).fontSize) || 0;
      if (size < 13.99) out.push({ tag: el.tagName.toLowerCase(), cls: String(el.className || "").slice(0, 60), size, text: own.slice(0, 50) });
    }
    return out;
  }, selector);
  check(hits.length === 0, `font floor 14 px on touch: ${what}`, hits.slice(0, 8));
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
  // ---------------------------------------------------------------- admin: sidebar, Accounts, modals, account page
  {
    const { ctx, page } = await newPage(browser);
    await signIn(page, "admin", ADMIN);
    // Sidebar (DESIGN-v2 §1): four groups in order, Setup at the bottom opens the guide.
    const nav = await page.evaluate(() => Array.from(document.querySelectorAll("#console-nav .af-nav-group")).map((g) => [g.querySelector(".af-nav-group__caption").textContent.trim(), Array.from(g.querySelectorAll(".tab-button")).map((b) => b.id.replace("tab-button-", ""))]));
    check(JSON.stringify(nav) === JSON.stringify([["Accounts", ["users", "workspaces"]], ["Work", ["workflows", "skills", "runtimes", "apps"]], ["Models", ["providers", "openai", "catalog", "defaults"]], ["System", ["models", "sandbox", "network"]]]), "sidebar groups in order", nav);
    check((await page.locator("#topbar-static #open-setup, #af-topbar-root [id*=setup]").count()) === 0, "no Setup button in the top bar");
    await page.click("#open-setup");
    await page.waitForSelector("#first-run-backdrop:not(.hidden)", { timeout: 10000 });
    check(true, "Setup (sidebar) opens the setup guide");
    await page.keyboard.press("Escape");
    await page.waitForSelector("#first-run-backdrop.hidden", { state: "attached", timeout: 5000 });
    await openAccount(page);
    await page.waitForSelector("#users-table tr[data-user='castor']");
    // Round 8: the three Email for everyone switches sit directly in the card (no Advanced disclosure).
    check(await page.locator("#email-caps-advanced").count() === 0, "Email for everyone: no Advanced disclosure");
    await page.waitForFunction(() => document.getElementById("email-cap-email").getAttribute("aria-checked") === "true", null, { timeout: 10000 });
    await labelScale(page, "#users-section", "accounts section");
    const caps = await page.evaluate(() => ["email-cap-email", "email-cap-agent-tools", "email-cap-recovery"].map((id) => { const b = document.getElementById(id); return [id, b.getAttribute("role"), b.getAttribute("aria-checked")]; }));
    check(JSON.stringify(caps) === JSON.stringify([["email-cap-email", "switch", "true"], ["email-cap-agent-tools", "switch", "true"], ["email-cap-recovery", "switch", "true"]]), "admin switches on by default", caps);
    // Accounts (§2): ONE table, users AND entities, tinted by kind, from GET /admin/accounts.
    const cols = await page.$$eval("#users-section thead th", (ths) => ths.map((t) => t.textContent.trim()));
    check(JSON.stringify(cols) === JSON.stringify(["Name", "Email", "Runtime", "Active", "Actions"]), "accounts columns (round 8: ONE Email column; the kind chip carries the role)", cols);
    const rows = await page.$$eval("#users-table tr.accounts-row", (trs) => trs.map((t) => [t.dataset.user, t.className.split(" ").find((c) => c.startsWith("af-row--")), getComputedStyle(t.cells[0]).backgroundColor]));
    check(JSON.stringify(rows.map((r) => r.slice(0, 2))) === JSON.stringify([["admin", "af-row--admin"], ["alice", "af-row--user"], ["bob", "af-row--user"], ["castor", "af-row--entity"]]), "one table: admin, users, entity rows with kind classes", rows);
    check(rows[0][2] !== "rgba(0, 0, 0, 0)" && rows[3][2] !== "rgba(0, 0, 0, 0)" && rows[0][2] !== rows[3][2], "admin and entity rows are tinted (kit tokens), differently", rows);
    check((await page.locator("#entities-list-section").isHidden()), "no separate Summoned entities panel for the admin");
    const own = await page.evaluate(() => { const b = document.querySelector("tr[data-user='admin'] .users-active [role=switch]"); const r = b && document.getElementById(b.id + "-reason"); return b && { dis: b.getAttribute("aria-disabled"), reason: r && !r.hidden ? r.textContent : null }; });
    check(own && own.dis === "true" && own.reason === "You can't deactivate your own account.", "own Active row unavailable with the reason", own);
    const alice = await page.textContent("tr[data-user='alice'] .accounts-email__text");
    check(alice.trim() === "alice@fastmail.com · connected", "alice Email cell: address · state", alice);
    check((await page.textContent("tr[data-user='bob'] .accounts-col-email")).trim() === "No address", "bob has no email address (words, not a dash)");
    // Entities are AI users with their own mailbox (DESIGN-v3 §3): the real state of its own plane.
    check((await page.textContent("tr[data-user='castor'] .accounts-email__text")).trim().endsWith("not connected") || (await page.textContent("tr[data-user='castor'] .accounts-email__text")).trim() === "No address", "entity Email cell reads its own plane's state");
    // DESIGN-v3 §1.1: only the actions that apply, no disabled buttons, no reasons paragraph.
    const acts = await page.evaluate(() => Object.fromEntries(Array.from(document.querySelectorAll("#users-table tr.accounts-row")).map((tr) => [tr.dataset.user, {
      vis: Array.from(tr.querySelectorAll(".accounts-actions__buttons > button")).map((b) => b.dataset.action),
      disabled: tr.querySelectorAll("button[disabled]").length,
    }])));
    // Round 8: icon actions, no "⋯" menu: users Email · OpenAI API · Logs · Workspace · Rotate · Archive.
    check(JSON.stringify(acts.castor) === JSON.stringify({ vis: ["email", "logs", "manage", "archive"], disabled: 0 }), "entity row: Email · Logs · Manage · Archive", acts.castor);
    check(JSON.stringify(acts.alice) === JSON.stringify({ vis: ["email", "openai_api", "logs", "workspace", "rotate", "archive"], disabled: 0 }), "user row: Email · OpenAI API · Logs · Workspace · Rotate · Archive", acts.alice);
    check(JSON.stringify(acts.admin) === JSON.stringify({ vis: ["email", "openai_api", "logs", "workspace", "rotate"], disabled: 0 }), "own row: no Archive offered", acts.admin);
    check((await page.locator("#users-section .accounts-reasons").count()) === 0 && !(await page.textContent("#users-section")).includes("don't apply to entities"), "no per-row reasons paragraph");
    // Round-2 polish: no placeholder dashes, actions on ONE row per account at 1440, one card title.
    const polish = await page.evaluate(() => {
      const cells = Array.from(document.querySelectorAll("#users-table tr.accounts-row td")).filter((td) => td.getClientRects().length).map((td) => td.innerText.trim());
      const text = document.getElementById("users-table").innerText;
      const rows = Array.from(document.querySelectorAll("#users-table tr.accounts-row")).map((tr) => {
        const tops = Array.from(tr.querySelectorAll(".accounts-actions__buttons > button")).map((b) => Math.round(b.getBoundingClientRect().top));
        const wrap = tr.closest(".users-table-wrap");
        return { id: tr.dataset.user, lines: new Set(tops).size, h: Math.round(tr.getBoundingClientRect().height), overflow: wrap.scrollWidth > wrap.clientWidth + 1 };
      });
      return { dashCells: cells.filter((c) => c === "—").length, dashPairs: /— ·|· —/.test(text), rows, titles: Array.from(document.querySelectorAll("#users-section h2, #users-section .section-title")).map((h) => h.textContent.trim()) };
    });
    check(polish.dashCells === 0 && !polish.dashPairs, "no placeholder dash cells and no '— · —' text in the accounts table", polish);
    check(polish.rows.every((r) => r.lines === 1 && !r.overflow && r.h < 100), "1440: every account's actions fit on one row (no wrap, no overflow, row < 100 px)", polish.rows);
    check(!polish.titles.includes("Accounts"), "no second 'Accounts' title in the card (the top bar carries it)", polish.titles);
    check((await page.locator("tr[data-user='castor'] button[data-action='manage']").count()) === 1 && (await page.locator("tr[data-user='alice'] button[data-action='manage']").count()) === 0, "Manage only on entity rows");
    // Active switch on a user row: inline confirmation, stays on until confirmed.
    await page.click("tr[data-user='bob'] .users-active [role=switch]");
    await page.waitForSelector("#users-table .row-confirm");
    check((await page.textContent("#users-table .row-confirm")).includes("Deactivate bob? They are signed out until you turn Active back on."), "inline deactivate confirmation");
    check((await page.getAttribute("tr[data-user='bob'] .users-active [role=switch]", "aria-checked")) === "true", "Active stays on until confirmed");
    await page.click("#users-table .row-confirm button.secondary");
    check((await page.locator("#users-table .row-confirm").count()) === 0, "Cancel closes the confirmation");
    // Active switch on an ENTITY row (§2.2): suspend asks, then the live API pauses it; resume restores.
    await page.click("tr[data-user='castor'] .users-active [role=switch]");
    await page.waitForSelector("#users-table .row-confirm");
    check((await page.textContent("#users-table .row-confirm")).includes("Suspend castor? It stops acting until you turn Active back on."), "entity suspend confirmation");
    await page.click("#users-table .row-confirm button.danger");
    await page.waitForFunction(() => document.querySelector("tr[data-user='castor'] .users-active [role=switch]")?.getAttribute("aria-checked") === "false", null, { timeout: 15000 });
    check((await page.textContent("#users-message")).includes("castor is suspended."), "entity suspended (live)", await page.textContent("#users-message"));
    await page.click("tr[data-user='castor'] .users-active [role=switch]");
    await page.waitForFunction(() => document.getElementById("users-message").textContent.includes("castor is active again"), null, { timeout: 15000 }).catch(() => {});
    check((await page.textContent("#users-message")).includes("castor is active again"), "entity resumed (live)", await page.textContent("#users-message"));
    // Email modal (§2.3): another user's row = the address only, never a mailbox form; Esc closes, focus returns.
    await page.click("tr[data-user='alice'] button[data-action='email']");
    await page.waitForSelector("#account-email-backdrop:not([hidden])");
    const other = await page.evaluate(() => ({ title: document.getElementById("account-email-title").textContent, form: !!document.querySelector("#account-email-body #my-email-password"), help: document.querySelector("#account-email-body .af-form__help").textContent, status: document.querySelector("#account-email-body .account-other-mailbox").textContent, focusInside: document.getElementById("account-email-backdrop").contains(document.activeElement), modal: document.querySelector("#account-email-backdrop .af-modal").getAttribute("aria-modal"), blur: getComputedStyle(document.getElementById("account-email-backdrop")).backdropFilter }));
    check(other.title === "Email — alice" && !other.form && other.help === "Where alice's sign-in codes and notifications go." && other.status.includes("connected as alice@fastmail.com"), "other user's Email modal: address only + read-only mailbox line", other);
    check(other.focusInside && other.modal === "true" && /blur/.test(other.blur), "modal: focus inside, aria-modal, blurred backdrop", other);
    await page.keyboard.press("Escape");
    await page.waitForSelector("#account-email-backdrop[hidden]", { state: "attached", timeout: 5000 });
    check(await page.evaluate(() => document.activeElement && document.activeElement.matches("tr[data-user='alice'] button[data-action='email']")), "Esc closes and focus returns to the row's Email button");
    // Entity row (DESIGN-v3 §3.2): the SAME account email UI, on the entity's own mailbox.
    const entReq = page.waitForRequest((r) => r.url().includes("/api/gateway/accounts/castor/email"), { timeout: 10000 }).then(() => true, () => false);
    await page.click("tr[data-user='castor'] button[data-action='email']");
    await page.waitForSelector("#account-email-backdrop:not([hidden]) #my-email-section");
    const ent = (await page.textContent("#account-email-body .account-modal-lead")).trim();
    check(ent === "castor is an AI user: this mailbox is its own. Its agents read and send from it; notifications about its runs go to its address." && (await entReq), "entity Email modal: the account email UI on /accounts/castor/email", ent);
    await page.click("#account-email-close");
    // Own row: the full email UI + the admin sentence; the page itself does not repeat it.
    check(await page.locator("#my-email-section").isHidden(), "admin's account page lives in the modal, not on the page");
    await page.click("tr[data-user='admin'] button[data-action='email']");
    await page.waitForSelector("#account-email-backdrop:not([hidden]) #my-email-section");
    check((await page.textContent("#account-email-body .account-modal-lead")).startsWith("You are also a user of this gateway"), "admin's own row adds the one sentence");
    // IMAP pane (§3): first + default; no User name / Display name; servers visible and pre-filled
    // at once, then replaced by discovery `defaults` unless edited.
    const tabs = await page.$$eval("#my-email-connect [role=tab]", (ts) => ts.map((t) => [t.textContent.trim(), t.getAttribute("aria-selected")]));
    check(JSON.stringify(tabs) === JSON.stringify([["IMAP", "true"], ["Google", "false"], ["Microsoft", "false"]]), "IMAP first and default", tabs);
    const editableAddr = () => page.evaluate(() => ["my-email-registered", "my-email-address", "my-email-oauth-address"].filter((id) => document.getElementById(id).checkVisibility()));
    let vis = await editableAddr();
    check(JSON.stringify(vis) === JSON.stringify(["my-email-address"]) && (await page.textContent("#my-email-registered-text")).startsWith("Not set yet"), "no address yet: the mailbox address is the ONE address field", vis);
    await page.click("#my-email-registered-change");
    vis = await editableAddr();
    check(JSON.stringify(vis) === JSON.stringify(["my-email-registered"]) && (await page.locator("#my-email-pane-imap .mailbox-address-line").isVisible()), "editing card 1 folds the mailbox field to its read-only line", vis);
    await page.fill("#my-email-registered", "admin@example.net");
    await page.click("#my-email-registered-save");
    await page.waitForFunction(() => document.getElementById("my-email-registered-text").textContent === "admin@example.net", null, { timeout: 10000 });
    vis = await editableAddr();
    const line = (await page.textContent("#my-email-pane-imap .mailbox-address-line")).trim();
    check(vis.length === 0 && line === "Mailbox account: admin@example.net Use a different account", "address set: read-only lines, no editable address field", { vis, line });
    await page.click("#my-email-pane-imap .mailbox-address-other");
    vis = await editableAddr();
    check(JSON.stringify(vis) === JSON.stringify(["my-email-address"]) && (await page.inputValue("#my-email-address")) === "admin@example.net", "Use a different account reveals the prefilled mailbox field, alone", vis);
    check(await page.evaluate(() => !document.getElementById("my-email-display-name") && document.getElementById("my-email-login-field").hidden && !Array.from(document.querySelectorAll("#my-email-pane-imap label")).some((l) => /user name|display name/i.test(l.textContent))), "no User name / Display name fields");
    await page.route("**/api/gateway/me/email/discover", async (route) => {
      await new Promise((r) => setTimeout(r, 600));
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ found: true, defaults: { imap: { host: "mail.example.org", port: 993, security: "ssl" }, smtp: { host: "mail.example.org", port: 587, security: "starttls" }, login: "me@example.org", source: "discovered", provider: null, message: "Settings found for example.org." } }) });
    });
    await page.fill("#my-email-address", "me@example.org");
    const instant = await page.evaluate(() => ["imap-host", "imap-port", "imap-security", "smtp-host", "smtp-port", "smtp-security"].map((k) => document.getElementById(`my-email-${k}`).value).concat(document.getElementById("my-email-servers-source").textContent));
    check(JSON.stringify(instant) === JSON.stringify(["imap.example.org", "993", "ssl", "smtp.example.org", "465", "ssl", "Standard settings for example.org — change them if your provider uses others."]), "servers pre-filled instantly with the standard values", instant);
    await page.fill("#my-email-imap-port", "1993");
    await page.waitForFunction(() => document.getElementById("my-email-servers-source").textContent === "Settings found for example.org.", null, { timeout: 10000 });
    const after = await page.evaluate(() => ["imap-host", "imap-port", "smtp-host", "smtp-port", "smtp-security"].map((k) => document.getElementById(`my-email-${k}`).value));
    check(JSON.stringify(after) === JSON.stringify(["mail.example.org", "1993", "mail.example.org", "587", "starttls"]), "discovery replaces the pre-fill, never an edited field", after);
    await page.unroute("**/api/gateway/me/email/discover");
    const rowCols = await page.evaluate(() => getComputedStyle(document.querySelector("#my-email-pane-imap .mail-server-row__fields")).gridTemplateColumns.split(" ").length);
    check(rowCols === 3, "Server · Port · Security on one row at 1440 px", rowCols);
    // Send a test: always the API's sentence, never a state name.
    await page.click("#my-email-notify-test");
    await page.waitForFunction(() => { const t = document.getElementById("my-email-notify-test-state").textContent; return t && t !== "Sending…"; }, null, { timeout: 20000 });
    const real = (await page.textContent("#my-email-notify-test-state")).trim();
    check(real.startsWith("Not sent:") && !/^(queued|failed|rate_limited)$/i.test(real), "test notification without a mailbox: a sentence (live)", real);
    await page.route("**/api/gateway/me/notifications/test", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ ok: true, sent: false, reason_code: "rate_limited", message: "Not sent: hourly limit reached (20 of 20 this hour) — resets at 14:05.", limit: { window: "hour", limit: 20, used: 20, resets_at: "2026-10-01T12:05:00Z" }, state: "queued" }) }));
    await page.click("#my-email-notify-test");
    await page.waitForFunction(() => document.getElementById("my-email-notify-test-state").textContent.startsWith("Not sent: hourly"), null, { timeout: 10000 });
    const limited = (await page.textContent("#my-email-notify-test-state")).trim();
    const localAt = await page.evaluate(() => { const d = new Date("2026-10-01T12:05:00Z"); return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`; });
    check(limited === `Not sent: hourly limit reached (20 of 20 this hour) — resets at ${localAt}.` && !/Z|T\d\d:/.test(limited), "rate_limited: a sentence with the viewer's local reset time, never 'queued' or ISO", limited);
    await page.unroute("**/api/gateway/me/notifications/test");
    await page.evaluate(() => { document.getElementById("my-email-advanced").open = true; });
    await labelScale(page, "#account-email-body", "email modal");
    const nf = await page.evaluate(() => ["my-email-notify-job-failed", "my-email-notify-approval", "my-email-agent-tools"].map((id) => { const b = document.getElementById(id); const r = document.getElementById(id + "-reason"); return [b.getAttribute("aria-disabled"), r.hidden ? "" : r.textContent]; }));
    check(nf.every(([d, r]) => d === "true" && r === "Connect a mailbox first."), "switches unavailable until a mailbox is connected", nf);
    await page.keyboard.press("Escape");
    await page.waitForSelector("#account-email-backdrop[hidden]", { state: "attached", timeout: 5000 });
    check(await page.evaluate(() => document.getElementById("my-email-section").closest("#account-email-body") === null), "the account page goes back home on close");
    // Logs modal (§2.4): filters, events from the audit log, the honest footer.
    await page.click("tr[data-user='alice'] button[data-action='logs']");
    await page.waitForSelector("#account-logs-backdrop:not([hidden]) .account-logs-item", { timeout: 10000 });
    const logs = await page.evaluate(() => ({ title: document.getElementById("account-logs-title").textContent, chips: Array.from(document.querySelectorAll("#account-logs-filters button")).map((b) => b.textContent), items: Array.from(document.querySelectorAll(".account-logs-item")).map((li) => li.querySelector(".account-logs-item__title").textContent), note: document.getElementById("account-logs-note").textContent }));
    check(logs.title === "Activity — alice" && JSON.stringify(logs.chips) === JSON.stringify(["All", "Sign-ins", "Runs", "Automations", "Email"]) && logs.items.some((t) => /mailbox/i.test(t)) && /not recorded/.test(logs.note), "Logs modal renders alice's events with the honest footer", logs);
    // Adversary pass 2 (F3): the footer in plain words (no HTTP verbs); a connection says how.
    check(logs.note.startsWith("The gateway records sign-ins, changes, runs started and email events. Page views and reads are not recorded") && !/POST|PUT|PATCH|DELETE/.test(logs.note), "Logs footer in plain words, no POST/PUT/PATCH/DELETE", logs.note);
    const details = await page.evaluate(() => Array.from(document.querySelectorAll(".account-logs-item")).map((li) => li.textContent));
    check(!details.some((t) => /\bpassword\b/.test(t) && !/password sign-in/.test(t)), "no bare 'password' event detail", details);
    const runLink = await page.evaluate(() => { const li = document.querySelector(".account-logs-item[data-kind='run']"); const a = li && li.querySelector(".account-logs-item__link"); return li && { title: li.querySelector(".account-logs-item__title").textContent, link: a && a.textContent, path: a && a.getAttribute("data-observer-path") }; });
    check(runLink && runLink.title === "Run started" && runLink.link === "Open in Observer" && runLink.path === `/apps/observer/#run/${RUN_ID}`, "a run event links into the Observer's run page (/apps/observer/#run/<run_id>)", runLink);
    await page.click("#account-logs-filters button:nth-child(2)");
    await page.waitForFunction(() => !document.getElementById("account-logs-message").textContent.startsWith("Loading"), null, { timeout: 10000 });
    check(await page.evaluate(() => Array.from(document.querySelectorAll(".account-logs-item")).every((li) => li.dataset.kind === "sign_in")), "the Sign-ins filter shows sign-ins only");
    await page.keyboard.press("Escape");
    // Create user modal.
    await page.click("#open-create-user");
    const adv = await page.evaluate(() => { const d = document.querySelector("#user-create-form details"); const r = document.getElementById("new-runtime"); return { open: d.open, runtimeShown: r.checkVisibility() }; });
    check(adv.open === false && adv.runtimeShown === false, "Create user Advanced collapsed by default", adv);
    await page.evaluate(() => { for (const d of document.querySelectorAll("#user-create-form details")) d.open = true; });
    await labelScale(page, "#user-create-form", "create user modal");
    check(await page.evaluate(() => !document.getElementById("new-email").closest("details")), "Email address at the top level of Create user");
    await page.click("#create-user-cancel");
    // Workflows (§4) on this FRESH data dir: plain names, purpose line, no orange wall.
    await page.evaluate(() => document.getElementById("tab-button-workflows").click());
    await page.waitForSelector("#workflows-table tr.workflows-row");
    await page.waitForSelector("#agent-defaults-root .agent-default", { timeout: 15000 });
    const wf = await page.evaluate(() => ({
      basic: document.querySelector("#workflows-table tr[data-bundle='basic-agent'] .workflows-name strong")?.textContent,
      what: document.querySelector("#workflows-table tr[data-bundle='basic-agent'] .workflows-what")?.textContent,
      source: document.querySelector("#workflows-table tr[data-bundle='basic-agent'] .workflows-source")?.textContent,
      warn: document.querySelectorAll("#tab-workflows .tone-warn").length,
      notAvailable: document.getElementById("tab-workflows").textContent.includes("Not available"),
      labels: Array.from(document.querySelectorAll("#agent-defaults-root .agent-default__name")).map((l) => l.textContent),
      purpose: document.querySelector("#tab-workflows .workflows-purpose")?.textContent || "",
    }));
    check(wf.basic === "Basic agent" && wf.what && wf.what !== "—" && wf.source === "Shipped", "workflow rows: plain name, what it does, source badge", wf);
    // Adversary pass 2 (F4): "No app" (not "None"); the gateway's own flows folder = shipped;
    // a 0.0.0 manifest version reads "unversioned".
    const wf2 = await page.evaluate(() => ({
      usedBy: Array.from(document.querySelectorAll("#workflows-table td.workflows-usedby")).map((td) => td.textContent.trim()),
      docs: document.querySelector("#workflows-table tr[data-bundle='docs-qa'] td.workflows-usedby")?.textContent.trim(),
      sources: Object.fromEntries(["map-reduce", "structured-extract", "adversarial-review", "meta-debate"].map((b) => [b, document.querySelector(`#workflows-table tr[data-bundle='${b}'] .workflows-source`)?.textContent])),
      orch: document.querySelector("#workflows-table tr[data-bundle='abstractassistant-orchestrator'] .workflows-version-cell")?.textContent,
    }));
    check(wf2.docs === "No app" && !wf2.usedBy.includes("None"), "Used by says 'No app', never 'None'", wf2);
    check(Object.values(wf2.sources).every((t) => t === "Shipped"), "bundles in the gateway's flows folder carry the 'Shipped' badge", wf2.sources);
    check(wf2.orch === "unversioned", "a 0.0.0 manifest version reads 'unversioned'", wf2.orch);
    check(wf.warn === 0 && !wf.notAvailable, "no warnings and no 'Not available' on a fresh install", wf);
    check(wf.labels.includes("AbstractCode — chat agent") && wf.labels.includes("Assistant"), "default workflow per app: plain names", wf.labels);
    check(wf.purpose.startsWith("Workflows are the programs your apps and automations run."), "purpose line");
    // Round-2 polish: a real table at 1440 — one compact row per bundle, no captioned blocks,
    // no second "Workflows" title, (?) a small glyph, Drafts / Older versions labelled by the feature.
    const table = await page.evaluate(() => {
      const t = document.querySelector("#tab-workflows .workflows-table");
      const rows = Array.from(t.querySelectorAll("tr.workflows-row"));
      const tall = rows.map((r) => [r.dataset.bundle, Math.round(r.getBoundingClientRect().height)]).filter(([, h]) => h >= 120);
      const what = rows[0].querySelector(".workflows-what");
      return { overflow: t.scrollWidth - t.parentElement.clientWidth, stacked: t.classList.contains("ui-stacked"), display: getComputedStyle(rows[0]).display, heads: Array.from(t.querySelectorAll("thead th")).filter((th) => th.getClientRects().length).length, tall, n: rows.length,
        captions: getComputedStyle(what, "::before").content, titles: Array.from(document.querySelectorAll("#workflows-section h2")).map((h) => h.textContent.trim()),
        help: Math.round(document.querySelector("#workflows-table .help-q > summary").getBoundingClientRect().height),
        switches: Array.from(document.querySelectorAll(".workflows-toolbar .af-switch__label")).map((l) => l.textContent) };
    });
    check(!table.stacked && table.display === "table-row" && table.heads === 7 && table.n > 5 && table.overflow <= 0, "1440: workflows is a table, one row per bundle, no sideways scroll", table);
    check(table.captions === "none" || table.captions === "normal", "no repeated per-cell captions at 1440", table.captions);
    check(!table.titles.includes("Workflows") && table.help <= 20, "no second 'Workflows' title; (?) is a small glyph", table);
    check(JSON.stringify(table.switches) === JSON.stringify(["Drafts", "Older versions", "Show archived"]), "toolbar switches labelled by the feature, not a verb", table.switches);
    // DESIGN-v3 §5: nothing is deletable; a shipped bundle has Export + Open in AbstractFlow and
    // (admin) the "Available to users" switch, never Delete or Archive; rows sit under the
    // "Shared with everyone" group.
    const shippedRow = await page.evaluate(() => {
      const r = document.querySelector("#workflows-table tr[data-bundle='basic-agent']");
      const labels = Array.from(r.querySelectorAll(".workflows-actions button")).map((b) => (b.textContent.trim() || b.title || "").replace(/ .*/, ""));  // round 8: icon buttons (tooltip = title)
      return { labels, sw: r.querySelector(".workflows-available [role=switch]")?.getAttribute("aria-checked"), groups: Array.from(document.querySelectorAll("#workflows-table tr.workflows-group .workflows-group__title")).map((t) => t.textContent), anyDelete: Array.from(document.querySelectorAll("#tab-workflows button")).some((b) => /^Delete/.test(b.textContent.trim())) };
    });
    check(JSON.stringify(shippedRow.labels) === JSON.stringify(["Export", "Open"]) && shippedRow.sw === "true" && !shippedRow.anyDelete && shippedRow.groups[0] === "Shared with everyone", "shipped workflow: Export + Open + availability switch (on); no Delete anywhere", shippedRow);
    // Streamed replies: a gateway-wide setting under "Settings", a kit switch labelled by the feature.
    const stream = await page.evaluate(() => { const b = document.querySelector("#agent-defaults-root [data-streaming-default]"); const box = b && b.closest(".workflows-settings"); return b && { role: b.getAttribute("role"), label: b.querySelector(".af-switch__label").textContent, heading: box && box.querySelector(".section-subtitle").textContent, checkbox: !!document.querySelector("#agent-defaults-root input[type=checkbox]") }; });
    check(stream && stream.role === "switch" && stream.label === "Streamed replies" && stream.heading === "Settings" && !stream.checkbox, "Streamed replies is a kit switch row under Settings", stream);
    await ctx.close();
  }
  // ---------------------------------------------------------------- phone: flat rows, one column, label scale + font floor
  {
    const { ctx, page } = await newPage(browser, { width: 390, height: 844 }, true);
    await signIn(page, "admin", ADMIN);
    await openAccount(page);
    await page.waitForSelector("tr[data-user='castor'].accounts-row");
    const row = await page.evaluate(() => { const tr = document.querySelector("tr[data-user='alice']"); const r = tr.getBoundingClientRect(); return { display: getComputedStyle(tr).display, w: Math.round(r.width), btnH: Math.min(...Array.from(tr.querySelectorAll(".accounts-actions button")).filter((b) => b.getClientRects().length).map((b) => b.getBoundingClientRect().height)) }; });
    check(row.display === "grid" && row.w >= 340 && row.btnH >= 44, "phone account rows are flat blocks with 44 px actions", row);
    await labelScale(page, "#users-section", "accounts (phone)");
    await page.click("tr[data-user='admin'] button[data-action='email']");
    await page.waitForSelector("#account-email-backdrop:not([hidden]) #my-email-section");
    const sheet = await page.evaluate(() => { const r = document.querySelector("#account-email-backdrop .af-modal").getBoundingClientRect(); return { w: Math.round(r.width), h: Math.round(r.height) }; });
    check(sheet.w === 390 && sheet.h >= 800, "email modal is a full-screen sheet on phones", sheet);
    const cols = await page.evaluate(() => getComputedStyle(document.querySelector("#my-email-pane-imap .mail-server-row__fields")).gridTemplateColumns.split(" ").length);
    check(cols === 2, "server rows wrap on phones (server full width, port + security)", cols);
    await labelScale(page, "#account-email-body", "email modal (phone)");
    await fontFloor(page, "#account-email-backdrop", "email modal (touch)");
    await page.keyboard.press("Escape");
    await fontFloor(page, "#tab-users", "accounts (touch)");
    await page.evaluate(() => document.getElementById("tab-button-workflows").click());
    await page.waitForSelector("#workflows-table tr.workflows-row");
    await page.waitForSelector("#agent-defaults-root .agent-default", { timeout: 15000 });
    await fontFloor(page, "#tab-workflows", "workflows (touch)");
    const helpTouch = await page.evaluate(() => { const s = document.querySelector("#workflows-table .help-q > summary"); const r = s.getBoundingClientRect(); const a = getComputedStyle(s, "::after"); return { h: Math.round(r.height), hitW: a.width, hitH: a.height }; });
    check(helpTouch.h <= 20 && helpTouch.hitW === "44px" && helpTouch.hitH === "44px", "touch: (?) stays a small glyph with a 44 px hit area", helpTouch);
    const phoneWf = await page.evaluate(() => { const r = document.querySelector("#workflows-table tr[data-bundle='basic-agent']"); return { meta: r.querySelector(".workflows-fold-meta").innerText.trim(), version: r.querySelector(".workflows-version-cell").getClientRects().length }; });
    check(/^Version \S+ ·\s*Shipped$/.test(phoneWf.meta) && phoneWf.version === 0, "phone: one 'Version · source' line, no captioned blocks", phoneWf);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
    check(!overflow, "no horizontal page scroll at 390 px");
    await page.click("#nav-toggle");
    await page.waitForFunction(() => document.body.classList.contains("nav-open"));
    check((await page.$$eval("#console-nav .af-nav-group__caption", (c) => c.map((x) => x.textContent.trim()))).join(",") === "Accounts,Work,Models,System", "phone drawer shows the same groups");
    await page.click("#open-setup");
    await page.waitForSelector("#first-run-backdrop:not(.hidden)", { timeout: 10000 });
    check(!(await page.evaluate(() => document.body.classList.contains("nav-open"))), "Setup from the drawer opens the guide and closes the drawer");
    await ctx.close();
  }
  // ---------------------------------------------------------------- alice: connected mailbox
  {
    const { ctx, page } = await newPage(browser);
    await signIn(page, "alice", ALICE);
    await openAccount(page);
    await page.waitForSelector("tr[data-user='alice'] button[data-action='email']", { timeout: 10000 });
    await page.click("tr[data-user='alice'] button[data-action='email']");
    await page.waitForSelector("#account-email-backdrop:not([hidden]) #my-email-section", { timeout: 10000 });
    await page.waitForSelector("#my-email-connected", { state: "visible", timeout: 10000 });
    const status = (await page.textContent("#my-email-status")).trim();
    check(status.startsWith("Connected as alice@fastmail.com · IMAP"), "connected status line", status);
    // RBAC: a user sees the Accounts table scoped to themself (GET /me/accounts), no admin tools.
    await page.waitForSelector("#users-table tr.accounts-row", { timeout: 10000 });
    const mine = await page.evaluate(() => ({ rows: Array.from(document.querySelectorAll("#users-table tr.accounts-row")).map((t) => t.dataset.user), createUser: document.getElementById("open-create-user").checkVisibility(), caps: document.getElementById("email-caps-section").checkVisibility(), createEntity: document.getElementById("accounts-create-entity").checkVisibility() }));
    check(JSON.stringify(mine) === JSON.stringify({ rows: ["alice"], createUser: false, caps: false, createEntity: true }), "non-admin Accounts: own row only, no Create user, no Email for everyone", mine);
    check((await page.getAttribute("#my-email-enabled", "aria-checked")) === "true" && (await page.textContent("#my-email-enabled .af-switch__label")) === "Active", "Active switch in the connected mailbox card");
    check(await page.locator("#my-email-oauth-cancel").isHidden(), "Cancel sign-in hidden when no sign-in is pending");
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
  // ---------------------------------------------------------------- bob: a user without an address (adversary pass 2, F1/F5/F6)
  {
    const { ctx, page } = await newPage(browser);
    await signIn(page, "bob", BOB);
    await openAccount(page);
    await page.waitForSelector("tr[data-user='bob'] button[data-action='email']", { timeout: 10000 });
    const head = await page.evaluate(() => ({ title: document.getElementById("page-title").textContent, sub: document.getElementById("page-subtitle").textContent }));
    check(head.title === "Your account" && head.sub === "Your account and the entities you created.", "non-admin page title: Your account", head);
    check(await page.locator("#my-email-section").isHidden(), "no inline 'My email address and mailbox' section on a user's page");
    check(await page.evaluate(() => !document.getElementById("tab-users").innerText.includes("My email address and mailbox")), "the old inline section title is gone from the page");
    // Round 8: the workspace policy left the account page for its own Workspaces page.
    check(await page.evaluate(() => !document.getElementById("my-workspace-policy-section") && !document.getElementById("tab-button-workspaces").classList.contains("hidden")), "no workspace policy on the account page; Workspaces is in the sidebar for a user too");
    await page.click("tr[data-user='bob'] button[data-action='email']");
    await page.waitForSelector("#account-email-backdrop:not([hidden]) #my-email-section", { timeout: 10000 });
    const card = await page.evaluate(() => ({ title: document.getElementById("account-email-title").textContent, text: document.getElementById("my-email-registered-text").textContent, link: document.getElementById("my-email-registered-change").textContent, shown: document.getElementById("my-email-registered-view").checkVisibility() }));
    check(card.title === "Email — bob" && card.shown && card.text === "Not set yet — connecting a mailbox below sets it." && card.link === "Set it now", "bob's Email modal: the address card is never empty", card);
    await page.keyboard.press("Escape");
    await page.waitForSelector("#account-email-backdrop[hidden]", { state: "attached", timeout: 5000 });
    check(await page.locator("#my-email-section").isHidden(), "closing the modal does not put the section back on the page");
    await ctx.close();
  }
} finally {
  await browser.close();
}
console.log(JSON.stringify({ failures, checks, labelScale: labelScaleOrigin }));
