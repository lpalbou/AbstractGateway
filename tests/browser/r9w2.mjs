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
  // W2/W3/non-admin (the Workspaces modals) moved to r11w2.mjs (round 11: no shared workspace).
  // ------------------------------------------------------------------ W4: the kit tooltip
  {
    const { ctx, page } = await open(browser);
    const tips = await page.evaluate(() => Object.fromEntries(Array.from(document.querySelectorAll("tr[data-user='alice'] .accounts-actions__buttons > button, tr[data-user='castor'] .accounts-actions__buttons > button")).map((b) => [`${b.closest("tr").dataset.user}:${b.dataset.action}`, { tip: b.dataset.afTip, title: b.getAttribute("title") }])));
    const want = {
      "alice:email": "Email address and mailbox of alice", "alice:openai_api": "OpenAI API access for alice", "alice:logs": "Activity log of alice",
      "alice:workspace": "Workspaces alice's agents may use", "alice:rotate": "Rotate alice's sign-in token", "alice:archive": "Archive alice (kept, hidden)",
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
    check(kb === "Workspaces alice's agents may use", "keyboard focus shows the tooltip", kb);
    await page.keyboard.press("Tab");
    await page.waitForTimeout(50);
    const moved = await page.evaluate(() => document.querySelector(".af-tooltip").hidden || document.querySelector(".af-tooltip").textContent !== "Workspaces alice's agents may use");
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
