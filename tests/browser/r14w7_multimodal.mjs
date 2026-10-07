// R14.7 browser checks of the Multimodal Capabilities grid, run by
// tests/test_r14w7_multimodal_layout_browser.py against a hermetic scratch gateway
// whose routes the test seeded (long model ids, faster-whisper large-v3 cached).
//
//   node r14w7_multimodal.mjs <base-url> <admin-token> <playwright-node-modules> [shots-dir]
//
// At 1680 (the operator's review width) / 1440 / 1280 / 834 px, light and dark:
//   - in EVERY row the route pill's box and the capability text's box do not
//     intersect, and the pill stays inside its own cell (table layout);
//   - the Weights cell is one pill + one short sentence (<= 3 text lines), its
//     full detail in the pill's kit tooltip (data-af-tip, shown on hover);
//   - no horizontal page scroll; 834 px shows the card layout;
//   - Voice Input reads "installed" + "In the Hugging Face cache." with the snapshot path.
// Prints one JSON line: {"failures": [...], "checks": N, "metrics": [...]}.
import { createRequire } from "node:module";
import path from "node:path";

const [BASE, TOKEN, PW, SHOTS] = process.argv.slice(2);
const require = createRequire(path.join(PW, "/"));
const { chromium } = require("playwright-core");

const failures = [];
let checks = 0;
const metrics = [];
function check(ok, what, detail) {
  checks += 1;
  if (!ok) failures.push(detail === undefined ? what : `${what}: ${JSON.stringify(detail)}`);
}

async function open(browser, theme, width) {
  const ctx = await browser.newContext({ viewport: { width, height: 1000 }, deviceScaleFactor: 1, colorScheme: theme });
  await ctx.addInitScript((t) => { try { localStorage.setItem("abstractgateway_ui_settings_v1", JSON.stringify({ theme: t })); } catch {} }, theme);
  const page = await ctx.newPage();
  page.on("pageerror", (e) => failures.push(`pageerror@${theme}/${width}: ${e.message}`));
  await page.goto(`${BASE}/console`);
  await page.waitForSelector("#login-form");
  await page.fill("#login-user", "admin");
  await page.fill("#login-token", TOKEN);
  await page.click("#login-button");
  await page.waitForFunction(() => document.body.classList.contains("signed-in"), null, { timeout: 20000 });
  await page.keyboard.press("Escape").catch(() => {});
  await page.evaluate(() => { document.body.classList.remove("nav-open"); document.getElementById("tab-button-defaults").click(); });
  // Rows rendered AND the weights probe answered (the voice row carries its pill).
  await page.waitForFunction(() => {
    const rows = [...document.querySelectorAll("#defaults-table > tr")];
    return rows.some((tr) => (tr.querySelector("td code") || {}).textContent === "input.voice" && tr.querySelector(".weights-pill"));
  }, null, { timeout: 30000 });
  await page.waitForTimeout(600); // the table layer's measuring frame
  return { ctx, page };
}

function measure() {
  const r = (el) => el.getBoundingClientRect();
  const inter = (a, b) => a.left < b.right - 0.5 && b.left < a.right - 0.5 && a.top < b.bottom - 0.5 && b.top < a.bottom - 0.5;
  const table = document.querySelector(".capability-table");
  const stacked = table.classList.contains("ui-stacked");
  const rows = [];
  for (const tr of document.querySelectorAll("#defaults-table > tr")) {
    const tds = tr.children;
    if (tds.length < 8) continue;
    const pill = tds[0].querySelector("code");
    const range = document.createRange();
    range.selectNodeContents(tds[1]);
    const cap = range.getBoundingClientRect();
    const pr = r(pill), c0 = r(tds[0]);
    const weightsPill = tds[4].querySelector(".weights-pill");
    const reason = tds[4].querySelector(".weights-reason");
    const lh = reason ? parseFloat(getComputedStyle(reason).lineHeight) || 18 : 18;
    rows.push({
      route: pill.textContent,
      fullKey: tds[0].querySelector("[title]") ? tds[0].querySelector("[title]").title : pill.textContent,
      overlap: inter(pr, cap),
      pillOutside: !stacked && (pr.right > c0.right + 0.5 || pr.left < c0.left - 0.5),
      capText: tds[1].textContent.trim(),
      weightsLabel: weightsPill ? weightsPill.textContent.trim() : null,
      weightsTip: weightsPill ? weightsPill.getAttribute("data-af-tip") : null,
      weightsTabbable: weightsPill ? weightsPill.tabIndex === 0 : null,
      reason: reason ? reason.textContent.trim() : null,
      reasonLines: reason ? Math.round(r(reason).height / lh) : 0,
      weightsHeight: Math.round(r(tds[4]).height),
      weightsText: tds[4].textContent.trim(),
    });
  }
  const doc = document.documentElement;
  return { stacked, rows, hscroll: doc.scrollWidth > doc.clientWidth + 1, bodyText: document.getElementById("defaults-section").textContent };
}

const browser = await chromium.launch({ headless: true });
try {
  for (const theme of ["light", "dark"]) {
    for (const width of [1680, 1440, 1280, 834]) {
      const { ctx, page } = await open(browser, theme, width);
      const m = await page.evaluate(measure);
      metrics.push({ theme, width, stacked: m.stacked, rows: m.rows.length, maxWeightsHeight: Math.max(...m.rows.map((x) => x.weightsHeight)) });
      const at = `${theme}/${width}`;
      check(m.rows.length >= 20, `${at} grid rendered every route`, m.rows.length);
      check(!m.hscroll, `${at} no horizontal page scroll`);
      if (width === 834) check(m.stacked, `${at} narrow width shows the card layout`);
      if (width >= 1280) check(!m.stacked, `${at} wide widths keep the table`);
      for (const row of m.rows) {
        check(!row.overlap, `${at} ${row.route}: route pill does not overlap the capability text`, row.capText);
        check(!row.pillOutside, `${at} ${row.route}: route pill stays inside its cell`);
        if (row.weightsLabel !== null) {
          check(row.reason && row.reason.length <= 40 && /[.]$/.test(row.reason), `${at} ${row.route}: weights say ONE short sentence`, row.reason);
          check(row.reasonLines <= 2, `${at} ${row.route}: the weights sentence fits two lines`, row.reasonLines);
          check(row.weightsTip && row.weightsTip.length > row.reason.length, `${at} ${row.route}: the full detail is the pill's kit tooltip`, row.weightsTip);
          check(row.weightsTabbable, `${at} ${row.route}: the weights pill is focusable (tooltip on keyboard)`);
          check(!row.weightsText.includes("Supported providers"), `${at} ${row.route}: no provider list in the cell`, row.weightsText);
        }
      }
      const actions = await page.evaluate(() => [...document.querySelectorAll("#defaults-table td.actions button")].map((b) => ({ text: b.textContent.trim(), tip: b.getAttribute("data-af-tip"), aria: b.getAttribute("aria-label") })));
      check(actions.length >= 10 && actions.every((a) => a.tip && a.aria && a.text.length <= 1), `${at} route actions are icon buttons with a kit tooltip`, actions.slice(0, 3));
      check(!m.bodyText.includes("no local-weights probe"), `${at} the old 'no local-weights probe' sentence is gone`);
      const voice = m.rows.find((x) => x.fullKey === "input.voice" || x.route === "input.voice");
      check(voice && voice.weightsLabel === "installed", `${at} Voice Input weights: installed`, voice);
      check(voice && voice.reason === "In the Hugging Face cache.", `${at} Voice Input sentence`, voice && voice.reason);
      check(voice && /models--Systran--faster-whisper-large-v3/.test(voice.weightsTip || ""), `${at} Voice Input tooltip names the snapshot`, voice && voice.weightsTip);
      const mystery = m.rows.find((x) => x.route === "input.music");
      check(mystery && mystery.reason === "mystery-engine is not checked.", `${at} unknown provider: named, short`, mystery && mystery.reason);
      if (theme === "light" && width === 1440) {
        // The kit tooltip actually shows on hover with the detail lines.
        const pill = page.locator("#defaults-table > tr", { has: page.locator("code", { hasText: /^input\.voice$/ }) }).locator(".weights-pill");
        await pill.scrollIntoViewIfNeeded();
        // Another row's tooltip (shown where the pointer last rested) can sit over this pill:
        // move away and let it close first (the kit hides it 100 ms after the pointer leaves).
        await page.mouse.move(0, 0);
        await page.waitForTimeout(400);
        await pill.hover();
        // More than one binder may own a tooltip node (the console's and the React islands'): read the visible one.
        const visibleTip = () => { const t = [...document.querySelectorAll(".af-tooltip")].find((x) => !x.hidden && getComputedStyle(x).visibility !== "hidden" && x.textContent.includes("Where:")); return t ? t.textContent : null; };
        await page.waitForFunction(visibleTip, null, { timeout: 3000 }).catch(() => {});
        const tip = await page.evaluate(visibleTip);
        check(tip && tip.includes("Where:") && tip.includes("faster-whisper-large-v3"), "hover shows the kit tooltip with the evidence path", tip);
        if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `multimodal-tooltip-${theme}-${width}.png`) });
        await page.mouse.move(0, 0);
      }
      if (SHOTS) {
        // The console scrolls inside its shell: a tall viewport shows the whole grid.
        await page.setViewportSize({ width, height: 3000 });
        await page.waitForTimeout(400);
        await page.screenshot({ path: path.join(SHOTS, `multimodal-${theme}-${width}.png`), fullPage: true });
      }
      await ctx.close();
    }
  }
} finally {
  await browser.close();
}
console.log(JSON.stringify({ failures, checks, metrics }));
