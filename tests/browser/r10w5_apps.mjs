// Browser checks of R10.6 (Apps rows show an available update), run by
// tests/test_gateway_console_browser_r10w5.py against a hermetic scratch gateway.
// GET /api/gateway/apps is answered by the page route with a FIXTURE in the
// gateway's shape (an Assistant with an update, a browser app with an update,
// an app started outside the gateway with a newer version); POST
// /api/gateway/apps/assistant/update is answered by the route too (nothing is
// installed). With MODE=live the overview is the gateway's own (no route) and
// nothing is clicked: screenshots only.
//
//   node r10w5_apps.mjs <base-url> <admin-token> <playwright-node-modules> [<shots-dir>] [live]
//
// Prints one JSON line: {"failures": [...], "checks": N}.
import { createRequire } from "node:module";
import path from "node:path";

const [BASE, ADMIN, PW, SHOTS, MODE] = process.argv.slice(2);
const LIVE = MODE === "live";
const require = createRequire(path.join(PW, "/"));
const { chromium } = require("playwright-core");

const failures = [];
let checks = 0;
function check(ok, what, detail) {
  checks += 1;
  if (!ok) failures.push(detail === undefined ? what : `${what}: ${JSON.stringify(detail)}`);
}

const TIP = "Install the newest Assistant (0.14.0); a running app restarts on it";
const EXTERNAL = "Started outside the gateway — update it where it was installed";
const FLOW_TIP = "Install the newest Flow Editor (0.8.0); a running app restarts on it";

let sourceCheckout = false;
const CHECKOUT = "Installed from a source checkout — update it there";

function overview() {
  const web = (id, name, extra) => Object.assign({
    id, name, kind: "web", description: name, package: `@abstractframework/${id}`, installed: true,
    version: "0.7.0", latest_version: "0.7.0", update_available: false, update_label: null, update_tip: null,
    running: false, status: "stopped", managed: true, source: "gateway", external: null, enabled: false,
    url: null, mounted: false, app_path: null, port: null, pid: null, restarts_last_minute: 0, last_exit_code: null,
    last_error: null, needs_node_install: false, install_available: false, install_blocked_reason: null,
    install_parts: ["web"], actions: ["launch", "logs"], active_job: null, log_path: null, content_summary: null,
    interfaces: [{ kind: "web" }],
    status_control: { label: "Stopped", tone: "muted", busy: false, action: "launch", enabled: true, tip: "Stopped — click to start" },
  }, extra);
  return {
    ok: true,
    runtime: { node: { available: true, version: "24.14.0", source: "system", install_available: false, message: "Node.js 24.14.0", problems: [], path: "/usr/bin/node", active_job: null } },
    apps: [
      web("observer", "Observer", { version: "0.7.0", latest_version: "0.8.0", update_available: true, update_tip: EXTERNAL,
        running: true, status: "running", managed: false, source: "external", url: "http://127.0.0.1:3001/", port: 3001, pid: 77,
        external: { port: 3001, pid: 77, version: "0.7.0", gateway_url: null, detail: "Started outside the gateway on port 3001" },
        actions: ["open"], status_control: { label: "Running", tone: "ok", busy: false, action: null, enabled: false, tip: "Started outside the gateway — stop it where it was started" } }),
      web("flow", "Flow Editor", { version: "0.7.0", latest_version: "0.8.0", update_available: true, update_label: "Update to 0.8.0", update_tip: FLOW_TIP,
        actions: ["launch", "update", "logs"] }),
      { id: "assistant", name: "Assistant", kind: "desktop", description: "A menu-bar assistant: chat or talk hands-free.", package: "abstractassistant",
        installed: true, version: "0.13.0", latest_version: "0.14.0", update_available: true, update_label: "Update to 0.14.0", update_tip: TIP,
        running: false, status: "stopped", managed: false, source: "script", external: null, enabled: false, url: null, port: null, pid: null,
        restarts_last_minute: 0, last_exit_code: null, last_error: null, needs_node_install: false, install_available: false,
        install_blocked_reason: null, install_parts: ["desktop"], actions: ["open", "update"], active_job: null, log_path: null,
        status_control: { label: "Stopped", tone: "muted", busy: false, action: "launch", enabled: true, tip: "Stopped — click to start" },
        content_summary: null, interfaces: [],
        desktop: { location: "/venv/bin/abstractassistant", found_by: ["script:/venv/bin/abstractassistant"], launch_command: "/venv/bin/abstractassistant",
          install_command: "uv pip install --python /venv/bin/python abstractassistant", launch_available: true, launch_blocked: null,
          launch_blocked_reason: null, other_running: null, restart_note: null, started_by_gateway: false, latest_error: null } },
    ],
    install_allowed: true,
    registry: { url: "https://registry.npmjs.org", reachable: true, error: null },
    gateway_url: BASE, console_tui: null, apps_host: "127.0.0.1", apps_path_prefix: "/apps/",
    data: { apps_dir: "/data/apps", node_dir: "/data/runtime/node", logs_dir: "/data/logs/apps" },
  };
}

function overviewNow() {
  const o = overview();
  if (sourceCheckout) {
    const a = o.apps.find((x) => x.id === "assistant");
    Object.assign(a, { actions: ["open"], update_label: null, update_tip: CHECKOUT });
    a.desktop.source_checkout = true;
  }
  return o;
}

const posted = [];

async function open(browser, { width = 1440, height = 900, theme = "dark" } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height } });
  await ctx.addInitScript((t) => { try { localStorage.setItem("abstractgateway_ui_settings_v1", JSON.stringify({ theme: t, font_scale: "md", header_density: "standard" })); localStorage.setItem("abstractgateway_active_tab_v1", "apps"); } catch {} }, theme);
  const page = await ctx.newPage();
  page.on("pageerror", (e) => failures.push(`pageerror@${width}/${theme}: ${e.message}`));
  if (!LIVE) {
    await page.route(/\/api\/gateway\/apps(\?.*)?$/, (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(overviewNow()) }));
    await page.route("**/api/gateway/apps/*/update", async (route) => {
      posted.push({ url: route.request().url(), method: route.request().method() });
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ok: true, created: true, job: { id: "j1", kind: "app_update", app_id: "assistant", title: "Update Assistant", state: "running", percent: 5, message: "Downloading and installing Assistant 0.14.0…", steps: [], parts: [] } }) });
    });
    await page.route("**/api/gateway/apps/jobs/*", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ok: true, job: { id: "j1", kind: "app_update", app_id: "assistant", title: "Update Assistant", state: "running", percent: 40, message: "Downloading and installing Assistant 0.14.0…", steps: [], parts: [] } }) }));
  }
  await page.goto(`${BASE}/console#apps`);
  await page.waitForSelector("#login-form");
  await page.fill("#login-user", "admin");
  await page.fill("#login-token", ADMIN);
  await page.click("#login-button");
  await page.waitForFunction(() => document.body.classList.contains("signed-in"), null, { timeout: 20000 });
  await page.keyboard.press("Escape").catch(() => {});
  await page.waitForSelector('[data-app-card="assistant"]', { timeout: 30000 });
  return { ctx, page };
}

const cardState = (page, id) => page.evaluate((id) => {
  const card = document.querySelector(`[data-app-card="${id}"]`);
  if (!card) return null;
  const upd = card.querySelector('.ui-card__actions [data-app-action="update"]');
  const anyUpdate = card.querySelectorAll('[data-app-action="update"]').length;
  const ext = card.querySelector("[data-app-update-external]");
  return {
    update: upd ? { text: upd.textContent.trim(), tip: upd.getAttribute("data-af-tip"), title: upd.getAttribute("title") } : null,
    anyUpdate,
    titles: Array.from(card.querySelectorAll("button[title]:not([disabled])")).map((b) => b.getAttribute("title")),
    external: ext ? ext.textContent.trim() : null,
    scrollW: document.documentElement.scrollWidth, vw: document.documentElement.clientWidth,
  };
}, id);

const browser = await chromium.launch();
try {
  for (const theme of ["dark", "light"]) {
    for (const width of [1440, 390]) {
      const { ctx, page } = await open(browser, { width, height: width < 500 ? 844 : 900, theme });
      const tag = `${width}/${theme}`;
      const a = await cardState(page, "assistant");
      const o = await cardState(page, "observer");
      if (!LIVE) {
        check(a && a.update && a.update.text === "Update to 0.14.0", `assistant Update in the action row ${tag}`, a);
        check(a && a.update && a.update.tip === TIP, `assistant Update kit tooltip ${tag}`, a && a.update);
        check(a && a.update && a.update.title === null, `no native title on Update ${tag}`, a && a.update);
        check(a && a.titles.length === 0, `no native title on any enabled card button ${tag}`, a && a.titles);
        check(o && o.anyUpdate === 0, `external row: no update action ${tag}`, o);
        check(o && o.external === `Latest 0.8.0 · ${EXTERNAL}`, `external row: Latest + sentence ${tag}`, o && o.external);
        const f = await cardState(page, "flow");
        check(f && f.update && f.update.text === "Update to 0.8.0" && f.update.tip === FLOW_TIP, `browser app Update ${tag}`, f && f.update);
      }
      check(a && a.scrollW <= a.vw + 1, `no horizontal scroll ${tag}`, a && [a.scrollW, a.vw]);
      if (SHOTS) {
        await page.locator('[data-app-card="assistant"]').scrollIntoViewIfNeeded();
        await page.screenshot({ path: `${SHOTS}/apps-${LIVE ? "live-" : ""}${width}-${theme}.png`, fullPage: true });
      }
      if (!LIVE && width === 1440) {
        // The kit tooltip shows the gateway's sentence on hover.
        await page.hover('[data-app-card="assistant"] .ui-card__actions [data-app-action="update"]');
        await page.waitForTimeout(400);
        const tipText = await page.evaluate(() => { const t = document.querySelector(".af-tooltip"); return t ? t.textContent.trim() : null; });
        check(tipText === TIP, `kit tooltip on hover ${tag}`, tipText);
        if (SHOTS) await page.screenshot({ path: `${SHOTS}/apps-update-tooltip-${width}-${theme}.png` });
        if (theme === "dark") {
          await page.click('[data-app-card="assistant"] .ui-card__actions [data-app-action="update"]');
          await page.waitForTimeout(600);
          check(posted.some((p) => p.method === "POST" && /\/api\/gateway\/apps\/assistant\/update$/.test(p.url)), "one click posts /apps/assistant/update", posted);
          if (SHOTS) await page.screenshot({ path: `${SHOTS}/apps-update-running-${width}-${theme}.png`, fullPage: true });
        }
      }
      await ctx.close();
    }
  }
  if (!LIVE) {
    // R10.6 F2: an Assistant installed from a source checkout: Latest + sentence, no Update.
    sourceCheckout = true;
    for (const theme of ["dark", "light"]) {
      const { ctx, page } = await open(browser, { width: 1440, height: 900, theme });
      const a = await cardState(page, "assistant");
      check(a && a.anyUpdate === 0, `source checkout: no update action ${theme}`, a);
      check(a && a.external === `Latest 0.14.0 · ${CHECKOUT}`, `source checkout: Latest + sentence ${theme}`, a && a.external);
      if (SHOTS) await page.screenshot({ path: `${SHOTS}/apps-source-checkout-1440-${theme}.png`, fullPage: true });
      await ctx.close();
    }
  }
} finally {
  await browser.close();
}
console.log(JSON.stringify({ failures, checks }));
