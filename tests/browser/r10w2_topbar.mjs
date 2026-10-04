// Browser checks of the R10.3 top-bar memory/compute widget, run by
// tests/test_gateway_console_browser_r10w2.py against a hermetic scratch gateway.
// GET /api/gateway/host/state is answered by the page route with FAKE numbers
// (no model is loaded, no real host figure is shown), and changed mid-test.
//
//   node r10w2_topbar.mjs <base-url> <admin-token> <playwright-node-modules> [<shots-dir>]
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

const GIB = 1024 ** 3;
function snapshot({ usedGib, pct, gpu, loaded }) {
  return {
    ok: true,
    ts: Date.now() / 1000,
    memory: {
      ram: { used_bytes: Math.round(usedGib * GIB), total_bytes: 64 * GIB, percent: pct },
      device: { backend: "metal", host_in_use_bytes: 18 * GIB, wired_limit_bytes: 48 * GIB, total_bytes: 64 * GIB },
      process: { rss_bytes: 3 * GIB },
    },
    gpu: { supported: true, utilization_gpu_pct: gpu, source: "ioreg" },
    models: Array.from({ length: loaded }, (_, i) => ({ provider: "mlx", model: `fake-model-${i + 1}`, resident: true, size_bytes: 15 * GIB, cache_bytes: GIB, task: "text_generation" })),
    session_caches: [{ bytes: 512 * 1024 ** 2 }],
    totals: { models: loaded, models_resident: loaded, model_bytes: 15 * GIB * loaded, cache_bytes_models: GIB * loaded, session_cache_bytes: 512 * 1024 ** 2 },
    degraded: [],
  };
}
let current = snapshot({ usedGib: 41.2, pct: 64.4, gpu: 12, loaded: 1 });
let hostStateCalls = 0;

async function open(browser, { width = 1440, height = 900, theme = "dark" } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height } });
  await ctx.addInitScript((t) => { try { localStorage.setItem("abstractgateway_ui_settings_v1", JSON.stringify({ theme: t, font_scale: "md", header_density: "standard" })); localStorage.setItem("abstractgateway_active_tab_v1", "users"); } catch {} }, theme);
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => { errors.push(e.message); failures.push(`pageerror@${width}/${theme}: ${e.message}`); });
  page.on("console", (m) => { if (m.type() === "error" && !/Failed to load resource/.test(m.text())) errors.push(m.text()); });
  await page.route("**/api/gateway/host/state", async (route) => {
    hostStateCalls += 1;
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(current) });
  });
  await page.goto(`${BASE}/console`);
  await page.waitForSelector("#login-form");
  await page.fill("#login-user", "admin");
  await page.fill("#login-token", ADMIN);
  await page.click("#login-button");
  await page.waitForFunction(() => document.body.classList.contains("signed-in"), null, { timeout: 20000 });
  await page.keyboard.press("Escape").catch(() => {});
  await page.waitForFunction(() => (document.getElementById("topbar-res-gpu") || {}).textContent === "12%", null, { timeout: 15000 }).catch(() => {});
  return { ctx, page, errors };
}

const widgetState = (page) => page.evaluate(() => {
  const b = document.getElementById("topbar-resources");
  const r = b.getBoundingClientRect();
  const island = document.getElementById("af-topbar-root").getBoundingClientRect();
  const vis = (id) => { const e = document.getElementById(id); return e && getComputedStyle(e).display !== "none"; };
  return {
    shown: getComputedStyle(b).display !== "none",
    mem: document.getElementById("topbar-res-mem").textContent,
    memShort: document.getElementById("topbar-res-mem-short").textContent,
    memLongVisible: vis("topbar-res-mem"), memShortVisible: vis("topbar-res-mem-short"),
    gpu: document.getElementById("topbar-res-gpu").textContent,
    models: document.getElementById("topbar-res-models").textContent,
    tip: b.getAttribute("data-af-tip"), title: b.getAttribute("title"), label: b.getAttribute("aria-label"),
    right: r.right, left: r.left, width: r.width, islandLeft: island.left, vw: document.documentElement.clientWidth,
    scrollW: document.documentElement.scrollWidth,
    address: !!document.getElementById("island-address") || !!document.getElementById("island-address-copy"),
  };
});

const browser = await chromium.launch();
try {
  for (const theme of ["dark", "light"]) {
    for (const width of [1440, 390]) {
      const { ctx, page, errors } = await open(browser, { width, height: width < 500 ? 844 : 900, theme });
      const w = await widgetState(page);
      const tag = `${width}/${theme}`;
      check(w.shown, `widget shown when signed in (${tag})`);
      check(!w.address, `no address + copy control in the top bar (${tag})`);
      check(w.gpu === "12%" && w.models === "1 model", `GPU and models figures (${tag})`, w);
      if (width >= 1024) {
        check(w.mem === "41.2 GiB / 64.0 GiB (64%)" && w.memLongVisible && !w.memShortVisible, `memory used/total (%) at ${tag}`, w);
        check(w.right <= w.islandLeft + 1, `widget sits left of the kit cluster (${tag})`, w);
      } else {
        check(w.memShort === "64%" && w.memShortVisible && !w.memLongVisible, `compact memory % at ${tag}`, w);
      }
      check(w.left >= 0 && w.right <= w.vw + 0.5 && w.scrollW <= w.vw + 1, `widget inside the viewport, no horizontal scroll (${tag})`, w);
      check(!w.title, `no native title (kit tooltip only) (${tag})`, w.title);
      check(/^RAM: 41\.2 GiB of 64\.0 GiB \(64%\)\nAccelerator heap: 18\.0 GiB \/ 48\.0 GiB \(all processes\)\nModel weights: 15\.0 GiB · 1 model loaded\nKV caches: 1\.0 GiB for models · 512\.0 MiB in sessions\nGPU load: 12% \(via ioreg\)\nClick to open Resources\.$/.test(w.tip || ""), `tooltip lists the real values (${tag})`, w.tip);

      // The kit tooltip, on hover: themed, multi-line, inside the viewport.
      await page.hover("#topbar-resources");
      await page.waitForTimeout(450);
      const tipLook = await page.evaluate(() => {
        const t = document.querySelector(".af-tooltip");
        if (!t || t.hidden) return null;
        const cs = getComputedStyle(t); const r = t.getBoundingClientRect();
        return { text: t.textContent, ws: cs.whiteSpace, lines: Math.round(r.height / parseFloat(cs.lineHeight || "20")), bg: cs.backgroundColor, fg: cs.color, inside: r.left >= 0 && r.right <= document.documentElement.clientWidth + 0.5 && r.top >= 0 };
      });
      check(tipLook && tipLook.text.startsWith("RAM: 41.2 GiB") && tipLook.ws === "pre-line" && tipLook.lines >= 6 && tipLook.bg !== tipLook.fg && tipLook.inside, `kit tooltip shows one value per line (${tag})`, tipLook);
      if (SHOTS) {
        await page.screenshot({ path: path.join(SHOTS, `topbar-${width}-${theme}-tooltip.png`) });
        await page.mouse.move(5, 300);
        await page.waitForTimeout(250);
        await page.screenshot({ path: path.join(SHOTS, `topbar-${width}-${theme}.png`) });
        await page.screenshot({ path: path.join(SHOTS, `topbar-${width}-${theme}-header.png`), clip: { x: 0, y: 0, width, height: width < 500 ? 140 : 90 } });
      }
      check(errors.length === 0, `no console error (${tag})`, errors);
      await ctx.close();
    }
  }

  // New data → the widget updates within a few seconds, without a reload.
  {
    const { ctx, page } = await open(browser, { width: 1440 });
    const before = hostStateCalls;
    current = snapshot({ usedGib: 50, pct: 78.1, gpu: 87, loaded: 2 });
    const t0 = Date.now();
    const ok = await page.waitForFunction(() => document.getElementById("topbar-res-gpu").textContent === "87%" && document.getElementById("topbar-res-models").textContent === "2 models", null, { timeout: 9000 }).then(() => true).catch(() => false);
    const w = await widgetState(page);
    check(ok && w.mem === "50.0 GiB / 64.0 GiB (78%)", "widget follows new Resources data on its own", { w, ms: Date.now() - t0 });
    check(hostStateCalls > before, "it re-reads GET /host/state (the Resources data)", { before, after: hostStateCalls });

    // A failing snapshot: dashes and the reason, never stale numbers.
    await page.unroute("**/api/gateway/host/state");
    await page.route("**/api/gateway/host/state", (route) => route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "host probe failed" }) }));
    const failed = await page.waitForFunction(() => document.getElementById("topbar-res-gpu").textContent === "—", null, { timeout: 9000 }).then(() => true).catch(() => false);
    const wf = await widgetState(page);
    check(failed && /Host resources unavailable/.test(wf.tip || ""), "a failed refresh shows dashes and the reason", wf);
    await page.unroute("**/api/gateway/host/state");
    await page.route("**/api/gateway/host/state", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(current) }));

    // A click opens the Resources page.
    await page.click("#topbar-resources");
    await page.waitForTimeout(400);
    const res = await page.evaluate(() => ({ active: document.getElementById("tab-models").classList.contains("active"), title: document.getElementById("page-title").textContent }));
    check(res.active && res.title === "Resources", "click opens the Resources page", res);
    // Keyboard: focus + Enter is a click.
    await page.click("#tab-button-users");
    await page.focus("#topbar-resources");
    await page.keyboard.press("Enter");
    await page.waitForTimeout(300);
    check(await page.evaluate(() => document.getElementById("tab-models").classList.contains("active")), "Enter on the focused widget opens Resources");
    // The address keeps its home on the Network page.
    await page.click("#tab-button-network");
    const net = await page.waitForSelector("[data-net-copy]", { timeout: 10000 }).then(() => true).catch(() => false);
    check(net, "Network page lists the addresses with Copy");
    if (SHOTS) await page.screenshot({ path: path.join(SHOTS, "network-1440-dark.png") });
    await ctx.close();
  }
} finally {
  await browser.close();
}
console.log(JSON.stringify({ failures, checks }));
