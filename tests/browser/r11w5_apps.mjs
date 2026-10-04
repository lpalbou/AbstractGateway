// Browser checks of R11.3 (the Apps status badge is the start/stop control),
// run by tests/test_gateway_console_browser_r11w5.py against a hermetic
// scratch gateway. GET /api/gateway/apps is answered by the page route with a
// STATEFUL fixture in the gateway's shape (a running and a stopped app the
// gateway manages, an app started outside the gateway, the Assistant started
// by the gateway); POST /apps/{id}/stop and /launch are answered by the route
// too and flip that app's state (nothing runs). With MODE=live the overview
// is the gateway's own (no route) and nothing is clicked: screenshots only.
//
//   node r11w5_apps.mjs <base-url> <admin-token> <playwright-node-modules> [<shots-dir>] [live]
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

const RUN_TIP = "Running — click to stop";
const START_TIP = "Stopped — click to start";
const QUIT_TIP = "Running — click to quit";
const EXTERNAL_TIP = "Started outside the gateway — stop it where it was started";
const ADMIN_TIP = "Only an admin can start or stop apps";

// The gateway's own badge rules (apps_manager.browser_status_control /
// _desktop_status_control), for the fixture's states.
let asAdmin = true;
const live = { flow: true, code: false, assistant: true };
let failNext = null;  // an app id whose next POST answers 409
let slowNext = false;  // the next POST waits (the pending badge is visible)

function badge(running, { desktop = false, external = false } = {}) {
  const label = running ? "Running" : "Stopped";
  const tone = running ? "ok" : "muted";
  if (external) return { label, tone, busy: false, action: null, enabled: false, tip: EXTERNAL_TIP };
  const action = running ? "stop" : "launch";
  if (!asAdmin) return { label, tone, busy: false, action, enabled: false, tip: ADMIN_TIP };
  return { label, tone, busy: false, action, enabled: true, tip: running ? (desktop ? QUIT_TIP : RUN_TIP) : START_TIP };
}

function web(id, name, running, extra) {
  return Object.assign({
    id, name, kind: "web", description: name, package: `@abstractframework/${id}`, installed: true,
    version: "0.7.0", latest_version: "0.7.0", update_available: false, update_label: null, update_tip: null,
    running, status: running ? "running" : "stopped", managed: true, source: "gateway", external: null, enabled: running,
    url: running ? "http://127.0.0.1:3105/" : null, mounted: running, app_path: running ? `/apps/${id}/` : null, port: running ? 3105 : null,
    pid: running ? 4242 : null, restarts_last_minute: 0, last_exit_code: null, last_error: null, needs_node_install: false,
    install_available: false, install_blocked_reason: null, install_parts: ["web"],
    actions: running ? ["open", "stop", "logs"] : ["launch", "logs"], active_job: null, log_path: null, content_summary: null,
    interfaces: [{ kind: "web" }], status_control: badge(running),
  }, extra || {});
}

function overview() {
  return {
    ok: true,
    runtime: { node: { available: true, version: "24.14.0", source: "system", install_available: false, message: "Node.js 24.14.0", problems: [], path: "/usr/bin/node", active_job: null } },
    apps: [
      web("flow", "Flow Editor", live.flow),
      web("code", "Code", live.code),
      web("observer", "Observer", true, {
        managed: false, source: "external", url: "http://127.0.0.1:3001/", port: 3001, pid: 77, mounted: false, app_path: null,
        external: { port: 3001, pid: 77, version: "0.7.0", gateway_url: null, detail: "Started outside the gateway on port 3001" },
        actions: ["open"], status_control: badge(true, { external: true }),
      }),
      { id: "assistant", name: "Assistant", kind: "desktop", description: "A menu-bar assistant: chat or talk hands-free.", package: "abstractassistant",
        installed: true, version: "0.13.0", latest_version: "0.13.0", update_available: false, update_label: null, update_tip: null,
        running: live.assistant, status: live.assistant ? "running" : "stopped", managed: false, source: "script", external: null, enabled: false, url: null, port: null,
        pid: live.assistant ? 5001 : null, restarts_last_minute: 0, last_exit_code: null, last_error: null, needs_node_install: false, install_available: false,
        install_blocked_reason: null, install_parts: ["desktop"], actions: live.assistant ? ["open", "stop"] : ["open"], active_job: null, log_path: null,
        content_summary: null, interfaces: [], status_control: badge(live.assistant, { desktop: true }),
        desktop: { location: "/venv/bin/abstractassistant", found_by: ["script:/venv/bin/abstractassistant"], launch_command: "/venv/bin/abstractassistant",
          install_command: "uv pip install --python /venv/bin/python abstractassistant", launch_available: asAdmin, launch_blocked: asAdmin ? null : "admin",
          launch_blocked_reason: asAdmin ? null : "Only an admin can start apps.", other_running: null, restart_note: null, started_by_gateway: live.assistant, latest_error: null } },
    ],
    install_allowed: true,
    registry: { url: "https://registry.npmjs.org", reachable: true, error: null },
    gateway_url: BASE, console_tui: null, apps_host: "127.0.0.1", apps_path_prefix: "/apps/",
    data: { apps_dir: "/data/apps", node_dir: "/data/runtime/node", logs_dir: "/data/logs/apps" },
  };
}

const posted = [];

async function open(browser, { width = 1440, height = 900, theme = "dark", tech = false } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height } });
  await ctx.addInitScript(([t, adv]) => {
    try {
      localStorage.setItem("abstractgateway_ui_settings_v1", JSON.stringify({ theme: t, font_scale: "md", header_density: "standard" }));
      localStorage.setItem("abstractgateway_show_advanced_v1", adv ? "1" : "0");
      localStorage.setItem("abstractgateway_active_tab_v1", "apps");
    } catch {}
  }, [theme, tech]);
  const page = await ctx.newPage();
  page.on("pageerror", (e) => failures.push(`pageerror@${width}/${theme}: ${e.message}`));
  if (!LIVE) {
    await page.route(/\/api\/gateway\/apps(\?.*)?$/, (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(overview()) }));
    await page.route(/\/api\/gateway\/apps\/[a-z]+\/(stop|launch)$/, async (route) => {
      const url = route.request().url();
      const [, id, verb] = url.match(/\/apps\/([a-z]+)\/(stop|launch)$/);
      posted.push({ id, verb, method: route.request().method() });
      if (slowNext) { slowNext = false; await new Promise((r) => setTimeout(r, 1500)); }
      if (failNext === id) {
        failNext = null;
        await route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ ok: false, reason: "launch_failed", message: `${id} did not start (exit code 1).`, hint: "Its log says why." }) });
        return;
      }
      live[id] = verb === "launch";
      const row = overview().apps.find((a) => a.id === id);
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(id === "assistant" ? { ok: true, app: row, message: verb === "stop" ? "The Assistant is stopped." : "The Assistant is starting." } : { ok: true, app: row }) });
    });
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

const badgeState = (page, id) => page.evaluate((id) => {
  const card = document.querySelector(`[data-app-card="${id}"]`);
  const b = card && card.querySelector("[data-app-badge]");
  if (!b) return null;
  return {
    tag: b.tagName, text: b.textContent.trim(), tip: b.getAttribute("data-af-tip"), title: b.getAttribute("title"),
    aria: b.getAttribute("aria-label"), ariaDisabled: b.getAttribute("aria-disabled"), disabled: b.disabled, busy: b.getAttribute("aria-busy"),
    action: b.getAttribute("data-badge-action"), tabIndex: b.tabIndex,
    stopButtons: card.querySelectorAll('[data-app-action="stop"], [data-app-action="launch"]').length,
    openButtons: card.querySelectorAll('.ui-card__actions [data-app-action="open"], .ui-card__actions [data-app-action="desktop-open"]').length,
    alert: (card.querySelector(".ui-alert") || { textContent: "" }).textContent.trim(),
  };
}, id);

const settle = (page) => page.waitForTimeout(500);
const focusIs = (page, id) => page.evaluate((id) => { const a = document.activeElement; return !!a && a.getAttribute("data-app-badge") === id; }, id);

const browser = await chromium.launch();
try {
  for (const theme of ["dark", "light"]) {
    for (const width of [1440, 390]) {
      Object.assign(live, { flow: true, code: false, assistant: true });
      asAdmin = true;
      const { ctx, page } = await open(browser, { width, height: width < 500 ? 844 : 900, theme });
      const tag = `${width}/${theme}`;
      const f = await badgeState(page, "flow");
      const c = await badgeState(page, "code");
      const o = await badgeState(page, "observer");
      const a = await badgeState(page, "assistant");
      if (!LIVE) {
        check(f && f.tag === "BUTTON" && f.text === "Running" && f.tip === RUN_TIP && f.aria === RUN_TIP && f.ariaDisabled === null && f.action === "stop", `flow: Running badge = stop control ${tag}`, f);
        check(f && f.title === null, `flow badge: no native title ${tag}`, f);
        check(f && f.stopButtons === 0 && f.openButtons === 1, `flow: no Stop/Start button, Open stays ${tag}`, f);
        check(c && c.text === "Stopped" && c.tip === START_TIP && c.action === "launch" && c.ariaDisabled === null, `code: Stopped badge = start control ${tag}`, c);
        check(o && o.text === "Running" && o.tip === EXTERNAL_TIP && o.ariaDisabled === "true" && o.tabIndex === 0, `observer: external badge disabled, focusable ${tag}`, o);
        check(a && a.text === "Running" && a.tip === QUIT_TIP && a.action === "stop", `assistant: Running (gateway-started) quits ${tag}`, a);
        check(a && a.openButtons === 1, `assistant: Open stays ${tag}`, a);
      }
      const sw = await page.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]);
      check(sw[0] <= sw[1] + 1, `no horizontal scroll ${tag}`, sw);
      if (SHOTS) {
        await page.locator('[data-app-card="flow"]').scrollIntoViewIfNeeded();
        await page.screenshot({ path: `${SHOTS}/apps-badge-${LIVE ? "live-" : ""}${width}-${theme}.png`, fullPage: true });
      }
      if (!LIVE) {
        // Hover: the kit tooltip says the gateway's words (desktop pointer only).
        if (width === 1440) {
          await page.hover('[data-app-card="flow"] [data-app-badge]');
          await page.waitForTimeout(400);
          const tipText = await page.evaluate(() => { const t = document.querySelector(".af-tooltip"); return t ? t.textContent.trim() : null; });
          check(tipText === RUN_TIP, `kit tooltip on the Running badge ${tag}`, tipText);
          if (SHOTS) await page.screenshot({ path: `${SHOTS}/apps-badge-tooltip-${width}-${theme}.png` });
          await page.hover('[data-app-card="observer"] [data-app-badge]');
          await page.waitForTimeout(400);
          const extTip = await page.evaluate(() => { const t = document.querySelector(".af-tooltip"); return t ? t.textContent.trim() : null; });
          check(extTip === EXTERNAL_TIP, `kit tooltip on the external badge ${tag}`, extTip);
          if (SHOTS) await page.screenshot({ path: `${SHOTS}/apps-badge-external-tooltip-${width}-${theme}.png` });
        }
        // One click on Running → POST /stop → Stopped.
        let before = posted.length;
        slowNext = true;
        await page.click('[data-app-card="flow"] [data-app-badge]');
        await page.waitForTimeout(400);
        const mid = await badgeState(page, "flow");
        check(mid && mid.text === "Stopping…" && mid.disabled === true && mid.busy === "true", `pending: Stopping… busy ${tag}`, mid);
        if (SHOTS && width === 1440) await page.screenshot({ path: `${SHOTS}/apps-badge-stopping-${width}-${theme}.png` });
        await page.waitForTimeout(1700);
        check(posted.length === before + 1 && posted[posted.length - 1].id === "flow" && posted[posted.length - 1].verb === "stop", `click Running posts /apps/flow/stop ${tag}`, posted.slice(before));
        const f2 = await badgeState(page, "flow");
        check(f2 && f2.text === "Stopped" && f2.tip === START_TIP, `flow badge now Stopped ${tag}`, f2);
        check(f2 && /Flow Editor is stopped\./.test(f2.alert), `flow: the result note ${tag}`, f2 && f2.alert);
        // One click on Stopped → POST /launch → Running.
        before = posted.length;
        await page.click('[data-app-card="code"] [data-app-badge]');
        await settle(page);
        check(posted.length === before + 1 && posted[posted.length - 1].id === "code" && posted[posted.length - 1].verb === "launch", `click Stopped posts /apps/code/launch ${tag}`, posted.slice(before));
        const c2 = await badgeState(page, "code");
        check(c2 && c2.text === "Running" && c2.tip === RUN_TIP, `code badge now Running ${tag}`, c2);
        // External: clicking (and Enter on it) sends nothing; it is focus-reachable.
        before = posted.length;
        await page.click('[data-app-card="observer"] [data-app-badge]', { force: true });
        await page.focus('[data-app-card="observer"] [data-app-badge]');
        check(await focusIs(page, "observer"), `external badge takes the focus ${tag}`);
        await page.keyboard.press("Enter");
        await settle(page);
        check(posted.length === before, `external badge sends nothing ${tag}`, posted.slice(before));
        // Keyboard: focus the flow badge (Stopped now), Enter starts it.
        before = posted.length;
        await page.focus('[data-app-card="flow"] [data-app-badge]');
        await page.keyboard.press("Enter");
        await settle(page);
        check(posted.length === before + 1 && posted[posted.length - 1].verb === "launch" && posted[posted.length - 1].id === "flow", `Enter on the focused badge starts it ${tag}`, posted.slice(before));
        // The Assistant started by the gateway: one click quits it.
        before = posted.length;
        await page.click('[data-app-card="assistant"] [data-app-badge]');
        await settle(page);
        check(posted.length === before + 1 && posted[posted.length - 1].id === "assistant" && posted[posted.length - 1].verb === "stop", `assistant badge posts /apps/assistant/stop ${tag}`, posted.slice(before));
        const a2 = await badgeState(page, "assistant");
        check(a2 && a2.text === "Stopped" && a2.tip === START_TIP && a2.action === "launch", `assistant now Stopped → its Open ${tag}`, a2);
        // A refused start: the appNotify error box, the badge unchanged.
        await page.click('[data-app-card="code"] [data-app-badge]');  // code is Running: stop succeeds
        await settle(page);
        failNext = "code";
        await page.click('[data-app-card="code"] [data-app-badge]');  // Stopped → launch → 409
        await settle(page);
        const c3 = await badgeState(page, "code");
        check(c3 && /Could not start Code: code did not start \(exit code 1\)\./.test(c3.alert) && c3.text === "Stopped", `refused start: error note, badge Stopped ${tag}`, c3);
        if (SHOTS && width === 1440) await page.screenshot({ path: `${SHOTS}/apps-badge-failed-${width}-${theme}.png`, fullPage: true });
      }
      await ctx.close();
    }
  }
  if (!LIVE) {
    // Technical details ON: still no Stop / Start button anywhere (the badge is the one control).
    Object.assign(live, { flow: true, code: false, assistant: true });
    asAdmin = true;
    {
      const { ctx, page } = await open(browser, { width: 1440, height: 900, theme: "dark", tech: true });
      const techOn = await page.evaluate(() => document.body.classList.contains("show-advanced"));
      check(techOn, "technical details are on for this pass");
      for (const id of ["flow", "code", "observer", "assistant"]) {
        const s = await badgeState(page, id);
        check(s && s.stopButtons === 0, `technical details: no Stop/Start button on ${id}`, s);
      }
      const ext = await page.evaluate(() => (document.querySelector('[data-app-external="observer"]') || { textContent: "" }).textContent.trim());
      check(ext === "Started outside the gateway on port 3001", "technical details: the external line stays", ext);
      if (SHOTS) await page.screenshot({ path: `${SHOTS}/apps-badge-tech-1440-dark.png`, fullPage: true });
      await ctx.close();
    }
    // A non-admin: every badge disabled with the sentence; nothing is sent.
    for (const theme of ["dark", "light"]) {
      Object.assign(live, { flow: true, code: false, assistant: true });
      asAdmin = false;
      const { ctx, page } = await open(browser, { width: 1440, height: 900, theme });
      for (const id of ["flow", "code", "assistant"]) {
        const s = await badgeState(page, id);
        check(s && s.ariaDisabled === "true" && s.tip === ADMIN_TIP, `non-admin: ${id} badge disabled with the sentence ${theme}`, s);
      }
      const before = posted.length;
      await page.click('[data-app-card="flow"] [data-app-badge]', { force: true });
      await settle(page);
      check(posted.length === before, `non-admin click sends nothing ${theme}`, posted.slice(before));
      if (SHOTS) await page.screenshot({ path: `${SHOTS}/apps-badge-non-admin-1440-${theme}.png`, fullPage: true });
      await ctx.close();
    }
  }
} finally {
  await browser.close();
}
console.log(JSON.stringify({ failures, checks }));
