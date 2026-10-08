// The console's table layer, in a real browser (tests/test_r15_table_layer_browser.py):
//  1. every body cell sits in its header's column (left/right within 1 px) on the Providers,
//     Accounts and Multimodal tables at 1680 / 1440 / 1280 px: an actions cell laid out as a
//     flex box left the column grid (its box no longer matched the header, the row border broke);
//  2. a table the layer turned into cards becomes a table again when there is room: widening
//     the viewport 834 -> 1440, and at 1440 when a transient wide value is gone (the layer used
//     to keep the width recorded when it stacked and stayed as cards).
//
//   node r15_table_layer.mjs <base-url> <admin-token> <playwright-node-modules>
// Prints one JSON line: {"failures": [...], "checks": N}.
import { createRequire } from "node:module";
import path from "node:path";

const [BASE, TOKEN, PW] = process.argv.slice(2);
const require = createRequire(path.join(PW, "/"));
const { chromium } = require("playwright-core");
const failures = [];
let checks = 0;
function check(ok, what, detail) { checks += 1; if (!ok) failures.push(detail === undefined ? what : `${what}: ${JSON.stringify(detail)}`); }

async function open(browser, width) {
  const ctx = await browser.newContext({ viewport: { width, height: 1200 }, deviceScaleFactor: 1 });
  const page = await ctx.newPage();
  page.on("pageerror", (e) => failures.push(`pageerror@${width}: ${e.message}`));
  await page.goto(`${BASE}/console`);
  await page.waitForSelector("#login-form");
  await page.fill("#login-user", "admin");
  await page.fill("#login-token", TOKEN);
  await page.click("#login-button");
  await page.waitForFunction(() => document.body.classList.contains("signed-in"), null, { timeout: 20000 });
  await page.keyboard.press("Escape").catch(() => {});
  return { ctx, page };
}
const tab = (page, id) => page.evaluate((x) => { document.body.classList.remove("nav-open"); document.getElementById(x).click(); }, id);
const misaligned = (page, selector) => page.evaluate((sel) => {
  const table = document.querySelector(sel);
  if (!table) return { missing: true };
  if (table.classList.contains("ui-stacked")) return { stacked: true };
  const ths = [...table.tHead.rows[0].cells].map((c) => c.getBoundingClientRect());
  const out = []; let cells = 0;
  for (const tr of table.tBodies[0].rows) {
    let col = 0;
    for (const td of tr.cells) {
      const span = td.colSpan || 1;
      if (span === 1 && ths[col] && ths[col].width) {
        cells += 1;
        const r = td.getBoundingClientRect(), h = ths[col];
        if (Math.abs(r.left - h.left) > 1 || Math.abs(r.right - h.right) > 1) out.push(`col ${col} (${td.className || "-"}) td ${Math.round(r.left)}-${Math.round(r.right)} vs th ${Math.round(h.left)}-${Math.round(h.right)}`);
      }
      col += span;
    }
  }
  return { cells, bad: out.slice(0, 4) };
}, selector);
const stacked = (page) => page.evaluate(() => document.querySelector(".capability-table").classList.contains("ui-stacked"));

const browser = await chromium.launch({ headless: true });
try {
  for (const width of [1680, 1440, 1280]) {
    const { ctx, page } = await open(browser, width);
    for (const [tabId, sel, ready] of [
      ["tab-button-providers", "#endpoint-profiles-table", "#endpoint-profiles-table td.actions"],
      ["tab-button-users", ".accounts-table", "#users-table td.actions, #users-table td[data-label='Actions']"],
      ["tab-button-defaults", ".capability-table", "#defaults-table .weights-pill"],
    ]) {
      await tab(page, tabId);
      await page.waitForSelector(ready, { timeout: 20000 });
      await page.waitForTimeout(700);
      const target = sel === "#endpoint-profiles-table" ? "table:has(> #endpoint-profiles-table)" : sel;
      const m = await misaligned(page, target);
      check(!m.missing && !m.stacked && m.cells > 0 && m.bad.length === 0, `${width} ${tabId}: every cell in its header's column`, m);
    }
    await ctx.close();
  }
  // Cards -> table.
  const { ctx, page } = await open(browser, 834);
  await tab(page, "tab-button-defaults");
  await page.waitForSelector("#defaults-table .weights-pill", { timeout: 20000 });
  await page.waitForTimeout(700);
  check(await stacked(page), "834: the Multimodal grid is cards");
  await page.setViewportSize({ width: 1440, height: 1200 });
  await page.waitForTimeout(800);
  check(!(await stacked(page)), "834 -> 1440: the grid is a table again");
  // A transient wide value stacks it; once it is gone the grid is a table again.
  await page.evaluate(() => { const td = document.querySelector("#defaults-table td.capability-source-cell"); const s = document.createElement("span"); s.id = "r15-wide"; s.style.whiteSpace = "nowrap"; s.textContent = "x".repeat(400); td.append(s); });
  await page.waitForTimeout(800);
  check(await stacked(page), "1440: a too-wide value turns the grid into cards");
  await page.evaluate(() => document.getElementById("r15-wide").remove());
  await page.waitForTimeout(800);
  check(!(await stacked(page)), "1440: the value gone, the grid is a table again (no stale width)");
  await ctx.close();
} finally {
  await browser.close();
}
console.log(JSON.stringify({ failures, checks }));
