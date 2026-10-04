// Browser checks of the Accounts page (DESIGN-v3 §1, §2, §3.2; round 8 R8.2: one Email column, the
// Runtime link, icon actions with tooltips, no "⋯"), run by
// tests/test_gateway_console_browser_accounts.py against a hermetic scratch gateway
// (admin, alice, bob, a long-id user, dave archived, the entity castor).
//
//   node accounts.mjs <base-url> <admin-token> <playwright-node-modules> <long-user-id>
//
// Prints one JSON line: {"failures": [...], "checks": N}.
import { createRequire } from "node:module";
import path from "node:path";

const [BASE, ADMIN, PW, LONG_ID] = process.argv.slice(2);
const require = createRequire(path.join(PW, "/"));
const { chromium } = require("playwright-core");

const failures = [];
let checks = 0;
function check(ok, what, detail) {
  checks += 1;
  if (!ok) failures.push(detail === undefined ? what : `${what}: ${JSON.stringify(detail)}`);
}

async function open(browser, width, height, { touch = false, showArchived = false } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height }, hasTouch: touch, isMobile: touch });
  await ctx.addInitScript((on) => { try { localStorage.setItem("abstractgateway.console.accounts.show_archived", on ? "1" : "0"); } catch {} }, showArchived);
  const page = await ctx.newPage();
  page.on("pageerror", (e) => failures.push(`pageerror@${width}: ${e.message}`));
  await page.goto(`${BASE}/console`);
  await page.waitForSelector("#login-form");
  await page.fill("#login-user", "admin");
  await page.fill("#login-token", ADMIN);
  await page.click("#login-button");
  await page.waitForFunction(() => document.body.classList.contains("signed-in"), null, { timeout: 20000 });
  await page.keyboard.press("Escape").catch(() => {});
  await page.evaluate(() => { document.body.classList.remove("nav-open"); document.getElementById("tab-button-users").click(); });
  await page.waitForSelector("#users-table tr[data-user='castor']", { timeout: 15000 });
  await page.waitForTimeout(300);
  return { ctx, page };
}

// A1 / R8.2: no page overflow, no inner horizontal scroller around the table, Actions inside the
// table, and (table mode) every row's actions on ONE line.
const LAYOUT = () => {
  const de = document.documentElement;
  const table = document.querySelector(".accounts-table");
  const tr = table.getBoundingClientRect();
  const cards = getComputedStyle(table.querySelector("thead")).display === "none";
  let over = -1e9;
  let multiLine = 0;
  for (const td of table.querySelectorAll("td.accounts-actions")) {
    const cell = td.getBoundingClientRect();
    const tops = new Set();
    for (const b of td.querySelectorAll(".accounts-actions__buttons > button")) {
      const r = b.getBoundingClientRect();
      over = Math.max(over, r.right - tr.right, r.right - cell.right);
      tops.add(Math.round(r.top));
    }
    if (tops.size > 1) multiLine += 1;
  }
  const scrollers = [];
  for (let el = table.parentElement; el && el !== document.body; el = el.parentElement) {
    const ox = getComputedStyle(el).overflowX;
    if ((ox === "auto" || ox === "scroll") && el.scrollWidth > el.clientWidth + 1) scrollers.push(el.id || el.className);
  }
  const wrap = table.closest(".users-table-wrap").getBoundingClientRect();
  return { sw: de.scrollWidth, iw: innerWidth, cards, over: Math.round(over), scrollers, tableOverWrap: Math.round(tr.right - wrap.right), wrapScroll: (() => { const el = table.closest(".users-table-wrap"); return el.scrollWidth - el.clientWidth; })(), multiLine, cols: Array.from(table.querySelectorAll("thead th")).map((t) => t.textContent.trim()) };
};

const browser = await chromium.launch({ headless: true });
try {
  // ---------------------------------------------------------------- widths (A1)
  // Table from ~900 px up (actions on one line), cards below.
  for (const [w, h, expectCards] of [[880, 900, true], [920, 900, false], [1024, 800, false], [1100, 800, false], [1280, 900, false], [1440, 900, false], [1680, 1000, false], [2000, 1000, false], [2560, 1200, false]]) {
    const { ctx, page } = await open(browser, w, h, { showArchived: true });
    const m = await page.evaluate(LAYOUT);
    check(m.sw <= m.iw, `${w}: the page does not scroll sideways`, m);
    check(m.scrollers.length === 0 && m.wrapScroll <= 1, `${w}: no inner horizontal scroller holds the table`, m);
    check(m.over <= 0.5 && m.tableOverWrap <= 0.5, `${w}: the Actions cell's right edge stays inside the table`, m);
    check(m.cards === expectCards, `${w}: ${expectCards ? "card list" : "table"} (cards below ~900 px)`, m);
    if (!expectCards) check(m.multiLine === 0, `${w}: every row's actions on one line`, m);
    check(JSON.stringify(m.cols) === JSON.stringify(["Name", "Email", "Runtime", "Active", "Actions"]), `${w}: columns Name · Email · Runtime · Active · Actions`, m.cols);
    await ctx.close();
  }
  for (const [w, h] of [[834, 1194], [390, 844]]) {
    const { ctx, page } = await open(browser, w, h, { touch: true });
    const m = await page.evaluate(LAYOUT);
    const row = await page.evaluate(() => { const tr = document.querySelector("tr[data-user='alice']"); return { display: getComputedStyle(tr).display, btnH: Math.min(...Array.from(tr.querySelectorAll(".accounts-actions button")).filter((b) => b.getClientRects().length).map((b) => b.getBoundingClientRect().height)) }; });
    check(m.cards && m.sw <= m.iw && row.display === "grid" && row.btnH >= 44, `${w}: flat card rows, 44 px actions, no sideways scroll`, { m, row });
    await ctx.close();
  }
  // ---------------------------------------------------------------- actions per kind (A2), archive (A3), entity email (A5)
  {
    const { ctx, page } = await open(browser, 1440, 900);
    const rows = await page.evaluate(() => Object.fromEntries(Array.from(document.querySelectorAll("#users-table tr.accounts-row")).map((tr) => [tr.dataset.user, {
      vis: Array.from(tr.querySelectorAll(".accounts-actions__buttons > button")).map((b) => b.dataset.action),
      tips: Array.from(tr.querySelectorAll(".accounts-actions__buttons > button")).map((b) => b.dataset.tip),
      icons: Array.from(tr.querySelectorAll(".accounts-actions__buttons > button")).every((b) => b.classList.contains("icon-btn") && b.textContent.trim() === "" && b.querySelector("svg") && b.getAttribute("aria-label") && !b.title),
      size: Array.from(tr.querySelectorAll(".accounts-actions__buttons > button")).map((b) => { const r = b.getBoundingClientRect(); return Math.min(Math.round(r.width), Math.round(r.height)); }),
      menus: tr.querySelectorAll(".af-menu, .af-menu__button").length,
      disabled: tr.querySelectorAll("button[disabled]").length,
      email: tr.querySelector("td.accounts-col-email .accounts-email__text").textContent,
    }])));
    check(!rows.dave, "archived accounts are hidden while Show archived is off", Object.keys(rows));
    check(Object.values(rows).every((r) => r.disabled === 0), "no disabled button in any account row", rows);
    check((await page.locator("#users-section .accounts-reasons").count()) === 0, "no reasons paragraph");
    check((await page.locator("#users-table [data-action='delete']").count()) === 0 && !(await page.textContent("#users-table")).includes("Delete"), "no Delete anywhere in the table");
    check(JSON.stringify(rows.alice.vis) === JSON.stringify(["email", "openai_api", "logs", "workspace", "rotate", "archive"]), "user: Email · OpenAI API · Logs · Workspace · Rotate · Archive", rows.alice);
    check(JSON.stringify(rows.alice.tips) === JSON.stringify(["Email", "OpenAI API: on", "Logs", "Workspace", "Rotate token", "Archive"]), "user: tooltips name each action", rows.alice.tips);
    check(JSON.stringify(rows.castor.vis) === JSON.stringify(["email", "logs", "manage", "archive"]), "entity: Email · Logs · Manage · Archive (no token, no Workspace)", rows.castor);
    check(JSON.stringify(rows.admin.vis) === JSON.stringify(["email", "openai_api", "logs", "workspace", "rotate"]), "own row: no Archive", rows.admin);
    check(Object.values(rows).every((r) => r.icons && r.menus === 0 && r.size.every((x) => x >= 44)), "icon buttons only: no label, no '⋯' menu, 44 px targets", rows);
    check(!(await page.textContent("#users-table")).includes("⋯"), "no '⋯' anywhere in the table");
    check(rows.alice.email === "alice@fastmail.com · not connected" && rows.bob.email === "No address", "ONE Email column: 'address · state' / 'No address'", [rows.alice.email, rows.bob.email]);
    // Tooltips: shown on hover and on keyboard focus (CSS from data-tip), never a native title.
    await page.hover("tr[data-user='alice'] button[data-action='workspace']");
    const tip = await page.evaluate(() => { const b = document.querySelector("tr[data-user='alice'] button[data-action='workspace']"); const cs = getComputedStyle(b, "::after"); return { display: cs.display, content: cs.content }; });
    check(tip.display === "block" && tip.content === '"Workspace"', "hovering an action shows its tooltip", tip);
    await page.mouse.move(5, 5);
    await page.focus("tr[data-user='alice'] button[data-action='logs']");
    await page.keyboard.press("Shift+Tab");
    await page.keyboard.press("Tab");
    const ftip = await page.evaluate(() => getComputedStyle(document.activeElement, "::after").display);
    check(ftip === "block", "keyboard focus shows the tooltip too", ftip);
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "an open tooltip does not widen the page");
    // Wrap, never truncate (operator rule): the long id and address are shown whole; the row grows.
    const wrapped = await page.evaluate((id) => { const tr = document.querySelector(`tr[data-user='${id}']`); const out = {}; for (const [k, sel] of [["name", ".accounts-name strong"], ["email", ".accounts-col-email .accounts-cell-text"], ["runtime", ".accounts-col-runtime .accounts-cell-text"]]) { const s = tr.querySelector(sel); const cs = getComputedStyle(s); const cell = s.closest("td").getBoundingClientRect(); const box = s.getBoundingClientRect(); out[k] = { text: s.textContent, clipped: box.right > cell.right + 1 || (cs.display !== "inline" && s.scrollWidth > s.clientWidth + 1) || cs.textOverflow === "ellipsis" || cs.whiteSpace === "nowrap", lines: s.getClientRects().length > 1 ? s.getClientRects().length : Math.round(box.height / (parseFloat(cs.lineHeight) || 20)) }; } return out; }, LONG_ID);
    check(Object.values(wrapped).every((c) => !c.clipped) && wrapped.email.text.startsWith("alexandra.konstantinopoulou@very-long") && wrapped.email.lines >= 2 && wrapped.name.text === LONG_ID, "long id / address / runtime wrap, never truncate", wrapped);
    // Rotate: an inline confirmation under the row (no dialog); Cancel leaves the token alone.
    await page.click("tr[data-user='alice'] button[data-action='rotate']");
    await page.waitForSelector("#users-table .row-confirm");
    check((await page.textContent("#users-table .row-confirm")).includes("Rotate the token of alice? The current token stops working now; the new one is shown once.") && (await page.locator("#confirm-backdrop:not(.hidden)").count()) === 0, "Rotate asks inline", await page.textContent("#users-table .row-confirm"));
    await page.click("#users-table .row-confirm button.secondary");
    check((await page.locator("#users-table .row-confirm").count()) === 0, "Cancel closes the inline question");
    // Archive bob: inline confirmation, then the row leaves the list (Show archived off).
    await page.click("tr[data-user='bob'] button[data-action='archive']");
    await page.waitForSelector("#users-table .row-confirm");
    check((await page.textContent("#users-table .row-confirm")).includes("Archive bob? They can't sign in any more. Their runtime, runs and history are kept; you can unarchive later."), "user archive confirmation sentence");
    await page.click("#users-table .row-confirm button.danger");
    await page.waitForFunction(() => !document.querySelector("tr[data-user='bob']"), null, { timeout: 10000 });
    check((await page.textContent("#users-message")).includes("bob is archived."), "archived message", await page.textContent("#users-message"));
    // Show archived (kit switch, admins): archived rows with the chip, no switch, Logs + Unarchive.
    const sw = page.locator("#accounts-show-archived");
    check((await sw.getAttribute("role")) === "switch" && (await sw.getAttribute("aria-checked")) === "false" && (await page.textContent("#accounts-archived-slot")).includes("Show archived"), "Show archived is a feature-labelled switch, off by default");
    await sw.click();
    await page.waitForSelector("tr[data-user='dave'][data-archived='true']", { timeout: 10000 });
    const dave = await page.evaluate(() => { const tr = document.querySelector("tr[data-user='dave']"); return { chip: !!tr.querySelector(".accounts-archived-chip"), active: tr.querySelector(".accounts-active").textContent.trim(), sw: !!tr.querySelector("[role=switch]"), vis: Array.from(tr.querySelectorAll(".accounts-actions__buttons > button")).map((b) => b.dataset.action) }; });
    check(JSON.stringify(dave) === JSON.stringify({ chip: true, active: "Archived", sw: false, vis: ["logs", "unarchive"] }), "archived row: chip, 'Archived' instead of the switch, Logs · Unarchive", dave);
    check(await page.evaluate(() => { try { return localStorage.getItem("abstractgateway.console.accounts.show_archived") === "1"; } catch { return false; } }), "Show archived is remembered for this viewer");
    await page.click("tr[data-user='bob'] button[data-action='unarchive']");
    await page.waitForFunction(() => document.getElementById("users-message").textContent.includes("bob is back, inactive"), null, { timeout: 10000 }).catch(() => {});
    check((await page.textContent("#users-message")).includes("bob is back, inactive: turn Active on to let it sign in."), "unarchive message", await page.textContent("#users-message"));
    check((await page.getAttribute("tr[data-user='bob'] .users-active [role=switch]", "aria-checked")) === "false", "an unarchived account comes back inactive");
    // Entity Email (§3.2): the same account email UI, on /accounts/castor/email.
    const req = page.waitForRequest((r) => r.url().includes("/api/gateway/accounts/castor/email"), { timeout: 10000 }).then(() => true, () => false);
    await page.click("tr[data-user='castor'] button[data-action='email']");
    await page.waitForSelector("#account-email-backdrop:not([hidden]) #my-email-section");
    const lead = (await page.textContent("#account-email-body .account-modal-lead")).trim();
    check(lead === "castor is an AI user: this mailbox is its own. Its agents read and send from it; notifications about its runs go to its address." && (await req), "entity Email = the account email UI on its own base", lead);
    check((await page.textContent("#my-email-registered-title")).trim() === "Email address" && !(await page.textContent("#account-email-body")).includes("can't have their own mailbox"), "entity voice: no 'Your email address', no 'no mailbox yet'");
    await page.keyboard.press("Escape");
    await page.waitForSelector("#account-email-backdrop[hidden]", { state: "attached" });
    const ownReq = page.waitForRequest((r) => r.url().endsWith("/api/gateway/me/email"), { timeout: 10000 }).then(() => true, () => false);
    await page.click("tr[data-user='admin'] button[data-action='email']");
    await page.waitForSelector("#account-email-backdrop:not([hidden]) #my-email-section");
    check((await ownReq) && (await page.textContent("#my-email-registered-title")).trim() === "Your email address", "closing restores the signed-in user's own base and voice");
    await ctx.close();
  }
  // ---------------------------------------------------------------- Create user + Create entity (adversary F1/F4)
  {
    const { ctx, page } = await open(browser, 1440, 900);
    // F1: Create user closes on Escape (kit modal), focus back on the opener.
    await page.click("#open-create-user");
    await page.waitForSelector("#user-create-backdrop:not(.hidden)");
    await page.keyboard.press("Escape");
    const cu = await page.evaluate(() => ({ hidden: document.getElementById("user-create-backdrop").classList.contains("hidden"), focus: document.activeElement && document.activeElement.id }));
    check(cu.hidden && cu.focus === "open-create-user", "Create user closes on Escape and focus returns to its button", cu);
    // F4: Create entity end to end: open, name, Validate & create, Summon, the row appears.
    await page.click("#accounts-create-entity", { timeout: 10000 });
    await page.waitForSelector("#entity-create-backdrop:not(.hidden)");
    await page.waitForFunction(() => document.getElementById("entity-template").options.length > 0, null, { timeout: 15000 });
    await page.fill("#entity-name", "Nova");
    await page.click("#entity-create");
    await page.waitForSelector("#confirm-backdrop:not(.hidden)", { timeout: 20000 });
    // The confirmation must be the visible top layer (it was painted under the Create entity dialog).
    const top = await page.evaluate(() => { const b = document.getElementById("confirm-ok"); const r = b.getBoundingClientRect(); const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2); return Boolean(hit && (hit === b || b.contains(hit))); });
    check(top, "the Summon confirmation is on top of the Create entity dialog");
    await page.click("#confirm-ok", { timeout: 10000 });
    const born = await page.waitForSelector("#users-table tr[data-user='nova']", { timeout: 60000 }).then(() => true, () => false);
    check(born, "Create entity: open → name → Validate & create → Summon → the row appears", (await page.textContent("#entity-create-message").catch(() => "")) || "");
    await page.keyboard.press("Escape");
    check(await page.evaluate(() => document.getElementById("entity-create-backdrop").classList.contains("hidden")), "Create entity closes on Escape");
    await ctx.close();
  }
  // ---------------------------------------------------------------- receive only (A20): row + modal status
  {
    const REASON = "No outgoing server: this mailbox is receive only — connect it again to send.";
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const page = await ctx.newPage();
    page.on("pageerror", (e) => failures.push(`pageerror@receive-only: ${e.message}`));
    await page.route(/\/api\/gateway\/admin\/accounts(\?.*)?$/, async (route) => {
      let res, j;
      try { res = await route.fetch(); j = await res.json(); } catch { return; }
      for (const r of j.accounts) if (r.id === "alice") r.mailbox = { state: "receive_only", address: "alice@fastmail.com", provider: "imap", reason: REASON };
      route.fulfill({ response: res, body: JSON.stringify(j) }).catch(() => {});
    });
    await page.route(/\/api\/gateway\/me\/email$/, async (route) => {
      if (route.request().method() !== "GET") return route.continue().catch(() => {});
      let res, j;
      try { res = await route.fetch(); j = await res.json(); } catch { return; }
      Object.assign(j, { configured: true, address: "admin@example.org", auth_kind: "password", imap: { host: "imap.example.org", port: 993, security: "ssl" }, smtp: null, send_capable: false, mailbox: { state: "receive_only", address: "admin@example.org", provider: "imap", reason: REASON } });
      route.fulfill({ response: res, body: JSON.stringify(j) }).catch(() => {});
    });
    await page.goto(`${BASE}/console`);
    await page.fill("#login-user", "admin");
    await page.fill("#login-token", ADMIN);
    await page.click("#login-button");
    await page.waitForFunction(() => document.body.classList.contains("signed-in"), null, { timeout: 20000 });
    await page.keyboard.press("Escape").catch(() => {});
    await page.evaluate(() => { document.body.classList.remove("nav-open"); document.getElementById("tab-button-users").click(); });
    await page.waitForSelector("tr[data-user='alice']");
    const cell = (await page.textContent("tr[data-user='alice'] td.accounts-col-email")).replace(/\s+/g, " ");
    check(cell.includes("alice@fastmail.com · receive only") && cell.includes(REASON) && !cell.includes("connected"), "receive-only mailbox: 'address · receive only' + the API's sentence in the row", cell);
    await page.click("tr[data-user='admin'] button[data-action='email']");
    await page.waitForSelector("#account-email-backdrop:not([hidden]) #my-email-section");
    await page.waitForFunction(() => document.getElementById("my-email-status").textContent.length > 0, null, { timeout: 10000 }).catch(() => {});
    const status = await page.evaluate(() => ({ s: document.getElementById("my-email-status").textContent, e: document.getElementById("my-email-status-error").textContent }));
    check(status.s.startsWith("Receive only as admin@example.org") && status.e.includes(REASON), "receive-only modal status: 'Receive only' + the API's sentence", status);
    await page.unrouteAll({ behavior: "ignoreErrors" });
    await ctx.close();
  }
} catch (e) {
  failures.push(`exception: ${String((e && e.message) || e).split("\n").slice(0, 6).join(" | ")}`);
} finally {
  await browser.close();
}
console.log(JSON.stringify({ failures, checks }));
