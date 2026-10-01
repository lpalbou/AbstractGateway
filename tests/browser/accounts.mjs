// Browser checks of the Accounts page (DESIGN-v3 §1, §2, §3.2), run by
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

// A1: no page overflow, no inner horizontal scroller around the table, Actions inside the table.
const LAYOUT = () => {
  const de = document.documentElement;
  const table = document.querySelector(".accounts-table");
  const tr = table.getBoundingClientRect();
  const cards = getComputedStyle(table.querySelector("thead")).display === "none";
  let over = -1e9;
  for (const td of table.querySelectorAll("td.accounts-actions")) {
    const cell = td.getBoundingClientRect();
    for (const b of td.querySelectorAll(".accounts-actions__buttons > button, .af-menu__button")) {
      const r = b.getBoundingClientRect();
      over = Math.max(over, r.right - tr.right, r.right - cell.right);
    }
  }
  const scrollers = [];
  for (let el = table.parentElement; el && el !== document.body; el = el.parentElement) {
    const ox = getComputedStyle(el).overflowX;
    if ((ox === "auto" || ox === "scroll") && el.scrollWidth > el.clientWidth + 1) scrollers.push(el.id || el.className);
  }
  const wrap = table.closest(".users-table-wrap").getBoundingClientRect();
  return { sw: de.scrollWidth, iw: innerWidth, cards, over: Math.round(over), scrollers, tableOverWrap: Math.round(tr.right - wrap.right) };
};

const browser = await chromium.launch({ headless: true });
try {
  // ---------------------------------------------------------------- widths (A1)
  for (const [w, h, expectCards] of [[1024, 800, true], [1100, 800, true], [1180, 800, true], [1280, 900, false], [1440, 900, false], [1680, 1000, false], [2000, 1000, false], [2560, 1200, false]]) {
    const { ctx, page } = await open(browser, w, h, { showArchived: true });
    const m = await page.evaluate(LAYOUT);
    check(m.sw <= m.iw, `${w}: the page does not scroll sideways`, m);
    check(m.scrollers.length === 0, `${w}: no inner horizontal scroller holds the table`, m);
    check(m.over <= 0.5 && m.tableOverWrap <= 0.5, `${w}: the Actions cell's right edge stays inside the table`, m);
    check(m.cards === expectCards, `${w}: ${expectCards ? "card list" : "table"} (breakpoint computed from the column minimums)`, m);
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
      menu: Array.from(tr.querySelectorAll(".af-menu__item")).map((b) => b.dataset.action),
      more: (tr.querySelector(".af-menu__button") || {}).title || "",
      disabled: tr.querySelectorAll("button[disabled]").length,
    }])));
    check(!rows.dave, "archived accounts are hidden while Show archived is off", Object.keys(rows));
    check(Object.values(rows).every((r) => r.disabled === 0), "no disabled button in any account row", rows);
    check((await page.locator("#users-section .accounts-reasons").count()) === 0, "no reasons paragraph");
    check((await page.locator("#users-table [data-action='delete']").count()) === 0 && !(await page.textContent("#users-table")).includes("Delete"), "no Delete anywhere in the table");
    check(JSON.stringify([rows.alice.vis, rows.alice.menu]) === JSON.stringify([["email", "logs", "workspace"], ["rotate", "archive"]]), "user: Email · Logs · Workspace, menu Rotate token · Archive", rows.alice);
    check(JSON.stringify([rows.castor.vis, rows.castor.menu]) === JSON.stringify([["email", "logs", "manage"], ["archive"]]), "entity: Email · Logs · Manage, menu Archive (no Workspace)", rows.castor);
    check(rows.castor.more.includes("no token to rotate"), "entity '⋯' says why Rotate is absent", rows.castor.more);
    check(JSON.stringify([rows.admin.vis, rows.admin.menu]) === JSON.stringify([["email", "logs", "workspace"], ["rotate"]]), "own row: no Archive", rows.admin);
    // Wrap, never truncate (operator rule): the long id and address are shown whole; the row grows.
    const wrapped = await page.evaluate((id) => { const tr = document.querySelector(`tr[data-user='${id}']`); const out = {}; for (const [k, sel] of [["name", ".accounts-name strong"], ["email", ".accounts-col-email .accounts-cell-text"], ["mailbox", ".accounts-mailbox__text"], ["runtime", ".accounts-col-runtime .accounts-cell-text"]]) { const s = tr.querySelector(sel); const cs = getComputedStyle(s); const cell = s.closest("td").getBoundingClientRect(); const box = s.getBoundingClientRect(); out[k] = { text: s.textContent, clipped: box.right > cell.right + 1 || (cs.display !== "inline" && s.scrollWidth > s.clientWidth + 1) || cs.textOverflow === "ellipsis" || cs.whiteSpace === "nowrap", lines: s.getClientRects().length > 1 ? s.getClientRects().length : Math.round(box.height / (parseFloat(cs.lineHeight) || 20)) }; } return out; }, LONG_ID);
    check(Object.values(wrapped).every((c) => !c.clipped) && wrapped.email.text.startsWith("alexandra.konstantinopoulou@very-long") && wrapped.email.lines >= 2 && wrapped.name.text === LONG_ID, "long id / address / runtime wrap, never truncate", wrapped);
    // The kit menu: ARIA, keyboard, Escape returns focus.
    const more = page.locator("tr[data-user='castor'] .af-menu__button");
    check((await more.getAttribute("aria-haspopup")) === "menu" && (await more.getAttribute("aria-label")) === "More actions for castor", "the '⋯' button is a labelled menu button");
    await more.click();
    check((await more.getAttribute("aria-expanded")) === "true" && (await page.evaluate(() => document.activeElement.dataset.action)) === "archive", "opening focuses the first item");
    await page.keyboard.press("ArrowDown");
    check((await page.evaluate(() => document.activeElement.dataset.action)) === "archive", "ArrowDown wraps on a one-item menu");
    await page.keyboard.press("Escape");
    check((await more.getAttribute("aria-expanded")) === "false" && (await page.evaluate(() => document.activeElement.getAttribute("aria-label"))) === "More actions for castor", "Escape closes and returns focus to '⋯'");
    // Archive bob: inline confirmation, then the row leaves the list (Show archived off).
    await page.click("tr[data-user='bob'] .af-menu__button");
    await page.click("tr[data-user='bob'] .af-menu__item[data-action='archive']");
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
    const dave = await page.evaluate(() => { const tr = document.querySelector("tr[data-user='dave']"); return { chip: !!tr.querySelector(".accounts-archived-chip"), active: tr.querySelector(".accounts-active").textContent.trim(), sw: !!tr.querySelector("[role=switch]"), vis: Array.from(tr.querySelectorAll(".accounts-actions__buttons > button")).map((b) => b.dataset.action), menu: Array.from(tr.querySelectorAll(".af-menu__item")).map((b) => b.dataset.action) }; });
    check(JSON.stringify(dave) === JSON.stringify({ chip: true, active: "Archived", sw: false, vis: ["logs"], menu: ["unarchive"] }), "archived row: chip, 'Archived' instead of the switch, Logs + menu Unarchive", dave);
    check(await page.evaluate(() => { try { return localStorage.getItem("abstractgateway.console.accounts.show_archived") === "1"; } catch { return false; } }), "Show archived is remembered for this viewer");
    await page.click("tr[data-user='bob'] .af-menu__button");
    await page.click("tr[data-user='bob'] .af-menu__item[data-action='unarchive']");
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
    const cell = (await page.textContent("tr[data-user='alice'] td.accounts-mailbox")).replace(/\s+/g, " ");
    check(cell.includes("Receive only — no outgoing server") && cell.includes(REASON) && !cell.includes("Connected as"), "receive-only mailbox: 'Receive only' + the API's sentence in the row", cell);
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
