// R13.1: the console's Resources page shows the last watchdog restart (Gateway card,
// "Last restart"), run by tests/test_r13_console_last_restart.py against a hermetic
// gateway whose data dir holds one incident file written before it started.
//
//   node r13_last_restart.mjs <base-url> <admin-token> <playwright-node-modules> [<shots-dir>]
//
// Prints one JSON line: {"failures": [...], "checks": N}.
import { createRequire } from "node:module";
import path from "node:path";

const [BASE, ADMIN, PW, SHOTS] = process.argv.slice(2);
const require = createRequire(path.join(PW, "/"));
const { chromium } = require("playwright-core");

const failures = [];
let checks = 0;
function check(ok, what, detail) {
  checks += 1;
  if (!ok) failures.push(detail === undefined ? what : `${what}: ${JSON.stringify(detail)}`);
}

async function open(browser, { width, height, theme }) {
  const ctx = await browser.newContext({ viewport: { width, height } });
  await ctx.addInitScript((t) => { try { localStorage.setItem("abstractgateway_ui_settings_v1", JSON.stringify({ theme: t, font_scale: "md", header_density: "standard" })); localStorage.setItem("abstractgateway_active_tab_v1", "models"); } catch {} }, theme);
  const page = await ctx.newPage();
  page.on("pageerror", (e) => failures.push(`pageerror@${width}/${theme}: ${e.message}`));
  await page.goto(`${BASE}/console`);
  await page.waitForSelector("#login-form");
  await page.fill("#login-user", "admin");
  await page.fill("#login-token", ADMIN);
  await page.click("#login-button");
  await page.waitForFunction(() => document.body.classList.contains("signed-in"), null, { timeout: 20000 });
  await page.keyboard.press("Escape").catch(() => {});
  await page.evaluate(() => { const b = document.getElementById("tab-button-models"); if (b) b.click(); });
  await page.waitForFunction(() => { const r = document.getElementById("gateway-host-hang-row"); return r && !r.classList.contains("hidden"); }, null, { timeout: 20000 }).catch(() => {});
  return { ctx, page };
}

const browser = await chromium.launch();
try {
  for (const theme of ["dark", "light"]) {
    for (const width of [1440, 390]) {
      const tag = `${width}/${theme}`;
      const { ctx, page } = await open(browser, { width, height: width < 500 ? 844 : 900, theme });
      const st = await page.evaluate(() => {
        const row = document.getElementById("gateway-host-hang-row");
        const r = row ? row.getBoundingClientRect() : null;
        return {
          shown: !!row && !row.classList.contains("hidden") && getComputedStyle(row).display !== "none",
          text: (document.getElementById("gateway-host-hang") || {}).textContent || "",
          dump: (document.getElementById("gateway-host-hang-dump") || {}).textContent || "",
          tip: (document.getElementById("gateway-host-hang") || { getAttribute: () => "" }).getAttribute("data-af-tip") || "",
          key: row ? row.querySelector(".entity-kv-key").textContent : "",
          left: r ? r.left : -1, right: r ? r.right : 0, vw: document.documentElement.clientWidth,
          scrollW: document.documentElement.scrollWidth,
        };
      });
      check(st.shown, `Last restart row shown (${tag})`, st);
      check(st.key === "Last restart", `row label (${tag})`, st.key);
      check(/^Gateway restarted at .+ after a hang — the event loop was blocked in starlette\/responses\.py:245 listen_for_disconnect \(called from abstractgateway\/security\/gateway_security\.py:1443 __call__\) while serving POST \/api\/gateway\/runs\/c45d73be\/voice\/tts\/stream$/.test(st.text), `the line names the time, the frame, the gateway frame and the request (${tag})`, st.text);
      check(st.dump.startsWith("Every thread's stack: ") && st.dump.endsWith(".threads.txt"), `the dump path (${tag})`, st.dump);
      check(st.tip.includes("Blocked 30.9 s in starlette/responses.py:245 listen_for_disconnect") && st.tip.includes("Stack dump: ") && st.tip.includes("Incident file: "), `kit tooltip names the top frame, the dump and the incident file (${tag})`, st.tip);
      check(st.left >= 0 && st.scrollW <= st.vw + 1, `inside the viewport, no horizontal scroll (${tag})`, st);
      if (SHOTS) {
        const card = await page.$("#gateway-host-hang-row");
        if (card) await card.scrollIntoViewIfNeeded();
        await page.screenshot({ path: path.join(SHOTS, `resources-last-restart-${theme}-${width}.png`), fullPage: false });
      }
      await ctx.close();
    }
  }
} finally {
  await browser.close();
}
console.log(JSON.stringify({ failures, checks }));
process.exit(failures.length ? 1 : 0);
