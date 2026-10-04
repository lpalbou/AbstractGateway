// Round 8 (R8.1) browser checks of the web console: the Apps page (no disclosures; Continuum's
// settings behind the gear on its card; "Apps settings" behind the toolbar gear; rows apply on
// blur / switch, no Save), the Skills shelf row, the Workflows rows (not expandable; Export /
// Open / Archive as icon buttons with tooltips in ONE row; inline description editing for the
// owner only), and "Email for everyone" (three switches in the card). Run by
// tests/test_gateway_console_browser_r8.py against a hermetic scratch gateway (admin + alice,
// alice owning one imported workflow).
//
//   node r8_console.mjs <base-url> <admin-token> <alice-token> <playwright-node-modules> [screenshot dir]
//
// Prints one JSON line: {"failures": [...], "checks": N, "shots": [...]}.
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";

const [BASE, ADMIN, ALICE, PW, SHOTS] = process.argv.slice(2);
if (!PW) throw new Error("r8_console.mjs <base> <admin-token> <alice-token> <playwright-node-modules> [shots dir]");
const require = createRequire(path.join(PW, "/"));
const { chromium } = require("playwright-core");

const failures = [];
const shots = [];
let checks = 0;
function check(ok, what, detail) {
  checks += 1;
  if (!ok) failures.push(detail === undefined ? what : `${what}: ${JSON.stringify(detail)}`);
}

async function newPage(browser, viewport, theme = "light") {
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: 1 });
  await ctx.addInitScript((t) => { try { localStorage.setItem("abstractgateway_ui_settings_v1", JSON.stringify({ theme: t })); } catch {} }, theme);
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
  await page.waitForTimeout(300);
}
async function openTab(page, tab) {
  await page.evaluate((t) => { document.body.classList.remove("nav-open"); document.getElementById(`tab-button-${t}`).click(); }, tab);
  await page.waitForSelector(`#tab-${tab}.active`);
}
async function noSideScroll(page, what) {
  const over = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  check(over <= 1, `no horizontal scroll: ${what}`, over);
}
// Full-page screenshots: the shell scrolls inside its own container, so it grows for the shot
// (then the style is removed again).
const GROW = "html,body,.shell,.shell_main,.shell_content{height:auto!important;max-height:none!important;overflow:visible!important}.shell_topbar,.shell_header{position:static!important}";
async function shot(page, name) {
  if (!SHOTS) return;
  fs.mkdirSync(SHOTS, { recursive: true });
  const file = path.join(SHOTS, `${name}.png`);
  const tag = await page.addStyleTag({ content: GROW });
  await page.waitForTimeout(150);
  await page.screenshot({ path: file, fullPage: true });
  await tag.evaluate((el) => el.remove());
  shots.push(file);
}
async function shotEl(page, selector, name) {
  if (!SHOTS) return;
  fs.mkdirSync(SHOTS, { recursive: true });
  const file = path.join(SHOTS, `${name}.png`);
  await page.locator(selector).screenshot({ path: file });
  shots.push(file);
}
async function api(page, method, p, body) {
  return page.evaluate(async ([m, url, b]) => {
    const csrf = (document.cookie.match(/(?:^|; )abstractgateway_csrf=([^;]+)/) || [])[1] || "";
    const r = await fetch(url, { method: m, headers: { "Content-Type": "application/json", "x-abstractgateway-csrf": decodeURIComponent(csrf) }, body: b ? JSON.stringify(b) : undefined, credentials: "same-origin" });
    return { status: r.status, body: await r.json().catch(() => ({})) };
  }, [method, `${BASE}/api/gateway${p}`, body]);
}
async function workflowsReady(page) {
  await openTab(page, "workflows");
  await page.waitForSelector("#workflows-table tr.workflows-row", { timeout: 30000 });
  await page.waitForTimeout(300);
}

const VIEWPORTS = [[1440, 900], [834, 1112], [390, 844]];
const browser = await chromium.launch();
try {
  // ------------------------------------------------------------------ Apps page (admin)
  {
    const { ctx, page } = await newPage(browser, { width: 1440, height: 900 });
    await signIn(page, "admin", ADMIN);
    await openTab(page, "apps");
    await page.waitForSelector('[data-app-card="continuum"]', { timeout: 30000 });
    check(await page.locator("#tab-apps details").count() === 0, "Apps page: no disclosure at all");
    check(await page.locator("#tab-apps").getByText("Advanced:", { exact: false }).count() === 0, "Apps page: no 'Advanced:' text");
    const gear = page.locator('[data-app-card="continuum"] .ui-card__actions [data-app-action="settings"]');
    check(await gear.count() === 1, "Continuum card: one gear in its action row");
    check((await gear.getAttribute("title")) === "Settings", "Continuum gear: tooltip Settings");
    check(await page.locator('[data-app-card="flow"] [data-app-action="settings"]').count() === 0, "Flow card: no gear (no settings of its own)");
    // The gear sits beside Open / Install in the same row.
    const same = await page.evaluate(() => {
      const row = document.querySelector('[data-app-card="continuum"] .ui-card__actions');
      const btns = Array.from(row.querySelectorAll("button"));
      const r = (b) => b.getBoundingClientRect();
      return btns.length >= 2 && btns.every((b) => Math.abs(r(b).top - r(btns[0]).top) < 2 && Math.abs(r(b).height - r(btns[0]).height) < 2);
    });
    check(same, "Continuum gear beside the primary action (one row)");
    await gear.click();
    await page.waitForSelector("#app-settings-backdrop:not([hidden]) [data-backlog-settings]");
    check((await page.textContent("#app-settings-title")).trim() === "Continuum settings", "modal title: Continuum settings");
    const modal = page.locator("#app-settings-backdrop");
    check(await modal.locator('input[data-backlog-input="triage_repo_root"]').count() === 1, "modal: backlog folder field");
    check(await modal.locator('[data-backlog-switch][role="switch"]').count() === 2, "modal: exec runner + process manager are switches");
    check(await modal.locator("button", { hasText: /^Save/ }).count() === 0, "modal: no Save button");
    check(await modal.getByText("Environment (legacy)").count() === 0, "modal: no Environment (legacy) source");
    await shotEl(page, "#app-settings-backdrop .af-modal", "continuum-modal-1440-light");
    // A switch applies at once and says Saved.
    await modal.locator('[data-backlog-switch="process_manager"]').click();
    await page.waitForSelector('[data-backlog-setting-saved="process_manager"]');
    check((await modal.locator('[data-backlog-setting-saved="process_manager"]').textContent()).trim() === "Saved", "process manager switch: Saved inline");
    let rc = await api(page, "GET", "/admin/runtime-config");
    check(rc.body.process_manager && rc.body.process_manager.value === true && rc.body.process_manager.source === "stored", "process manager stored on", rc.body.process_manager);
    check((await modal.locator('[data-backlog-switch="process_manager"]').getAttribute("aria-checked")) === "true", "switch shows the stored state");
    // The folder applies on blur: a missing folder is refused with the gateway's sentence.
    const folder = modal.locator('input[data-backlog-input="triage_repo_root"]');
    await folder.fill("/definitely/not/a/folder-r8");
    await page.locator("#app-settings-title").click();
    await page.waitForSelector('[data-backlog-setting-saved="triage_repo_root"]');
    const refusal = (await modal.locator('[data-backlog-setting-saved="triage_repo_root"]').textContent()).trim();
    check(refusal.startsWith("Not saved:"), "folder blur: refusal shown inline", refusal);
    rc = await api(page, "GET", "/admin/runtime-config");
    check(rc.body.triage_repo_root.source === "default", "refused folder not stored", rc.body.triage_repo_root.source);
    // Escape closes the modal.
    await page.keyboard.press("Escape");
    await page.waitForTimeout(200);
    check(await page.locator("#app-settings-backdrop[hidden]").count() === 1, "Escape closes the modal");
    // The toolbar gear: Apps settings.
    const tgear = page.locator('#apps-root .ui-toolbar [data-app-action="apps-settings"]');
    check(await tgear.count() === 1 && (await tgear.getAttribute("title")) === "Apps settings", "Apps toolbar: gear with tooltip Apps settings");
    await tgear.click();
    await page.waitForSelector("#app-settings-backdrop:not([hidden]) [data-apps-settings]");
    check((await page.textContent("#app-settings-title")).trim() === "Apps settings", "modal title: Apps settings");
    check(await modal.locator("button", { hasText: /Save/ }).count() === 0, "Apps settings: no Save button");
    check(await modal.locator('input[data-apps-input="host"]').count() === 0, "Apps settings: the deprecated 'Where apps listen' row is hidden");
    const ports = modal.locator('input[data-apps-input="ports"]');
    await ports.fill("3100-3150");
    await ports.press("Enter");
    await page.waitForSelector('[data-apps-setting="ports"] [data-apps-setting-saved]');
    check((await modal.locator('[data-apps-setting="ports"] [data-apps-setting-saved]').textContent()).trim() === "Saved", "apps.ports: Saved inline on Enter");
    rc = await api(page, "GET", "/admin/runtime-config");
    check(rc.body.apps.ports.value === "3100-3150" && rc.body.apps.ports.source === "stored", "apps.ports stored", rc.body.apps.ports);
    await shotEl(page, "#app-settings-backdrop .af-modal", "apps-settings-modal-1440-light");
    await page.click("#app-settings-close");
    await ctx.close();
  }

  // ------------------------------------------------------------------ Skills shelf row (admin)
  {
    const { ctx, page } = await newPage(browser, { width: 1440, height: 900 });
    await signIn(page, "admin", ADMIN);
    await openTab(page, "skills");
    await page.waitForSelector("#skills-settings-root [data-skills-shelf]", { timeout: 30000 });
    check(await page.locator("#skmcp-pane-skills details").count() === 0, "Skills tab: no disclosure");
    const row = await page.evaluate(() => {
      const r = document.querySelector("#skills-settings-root [data-skills-shelf]");
      const input = r.querySelector("input"); const btn = r.querySelector("[data-skills-shelf-reseed]");
      return { input: !!input, btn: btn && btn.textContent.trim(), sameRow: input && btn && Math.abs(input.getBoundingClientRect().top + input.getBoundingClientRect().height / 2 - (btn.getBoundingClientRect().top + btn.getBoundingClientRect().height / 2)) < 6 };
    });
    check(row.input && row.btn === "Refresh curated shelf" && row.sameRow, "Skills shelf: field + Refresh curated shelf on one row", row);
    await ctx.close();
  }

  // ------------------------------------------------------------------ Email for everyone (admin)
  {
    const { ctx, page } = await newPage(browser, { width: 1440, height: 900 });
    await signIn(page, "admin", ADMIN);
    await openTab(page, "users");
    await page.waitForSelector("#email-caps-section");
    check(await page.locator("#email-caps-advanced").count() === 0, "Email for everyone: no Advanced disclosure");
    for (const id of ["email-cap-email", "email-cap-agent-tools", "email-cap-recovery"]) {
      check(await page.locator(`#${id}`).isVisible(), `Email for everyone: #${id} visible without opening anything`);
    }
    await ctx.close();
  }

  // ------------------------------------------------------------------ Workflows
  {
    const { ctx, page } = await newPage(browser, { width: 1440, height: 900 });
    await signIn(page, "alice", ALICE);
    await workflowsReady(page);
    const mine = page.locator('#workflows-table tr.workflows-row[data-bundle="r8-alice-wf"][data-owner="user"]');
    check(await mine.count() === 1, "alice: her workflow is listed under Mine");
    check(await page.locator(".workflows-chevron, tr.workflows-detail").count() === 0, "no ▸ chevron, no detail row");
    const before = await page.locator("#workflows-table tr").count();
    await mine.locator("td.workflows-version-cell").click();
    await page.waitForTimeout(200);
    check(await page.locator("#workflows-table tr").count() === before, "clicking a row unfolds nothing");
    check((await mine.getAttribute("aria-expanded")) === null && (await mine.getAttribute("tabindex")) === null, "a row is not a button");
    // Icon buttons with tooltips, one row.
    const acts = await mine.locator("td.workflows-actions button").evaluateAll((bs) => bs.map((b) => ({ title: b.title, label: b.getAttribute("aria-label"), text: b.textContent.trim(), top: Math.round(b.getBoundingClientRect().top), w: Math.round(b.getBoundingClientRect().width), h: Math.round(b.getBoundingClientRect().height) })));
    check(acts.map((a) => a.title).join("|") === "Export|Open in AbstractFlow|Archive", "actions: Export · Open · Archive tooltips", acts.map((a) => a.title));
    check(acts.every((a) => a.text === "" && a.label), "actions: icon-only with an aria-label", acts);
    check(new Set(acts.map((a) => a.top)).size === 1, "actions: one row", acts);
    check(acts.every((a) => a.w >= 44 && a.h >= 44), "actions: 44 px targets", acts);
    // Shared (gateway) workflow: no pencil for alice; hers: a pencil.
    const shared = page.locator('#workflows-table tr.workflows-row[data-owner="gateway"]').first();
    check(await shared.locator("td.workflows-what .workflows-desc-edit").count() === 0, "alice: no pencil on a shared workflow");
    const pencil = mine.locator("td.workflows-what .workflows-desc-edit");
    check(await pencil.count() === 1 && (await pencil.getAttribute("title")) === "Edit description", "alice: pencil on her workflow");
    await shot(page, "workflows-alice-1440-light");
    await pencil.click();
    const area = mine.locator("td.workflows-what textarea.workflows-desc-input");
    check(await area.count() === 1, "pencil opens a textarea");
    await area.fill("Turns my notes into a weekly digest.");
    await area.press("Enter");
    await page.waitForFunction(() => {
      const r = document.querySelector('#workflows-table tr.workflows-row[data-bundle="r8-alice-wf"] td.workflows-what');
      return r && /Turns my notes into a weekly digest\./.test(r.textContent) && !r.querySelector("textarea");
    }, null, { timeout: 15000 });
    check((await mine.locator("td.workflows-what .workflows-desc-note").textContent()).trim() === "Saved", "description: Saved inline");
    const list = await api(page, "GET", "/bundles?all_versions=true");
    const item = (list.body.items || []).find((it) => it.bundle_id === "r8-alice-wf");
    check(item && item.description === "Turns my notes into a weekly digest." && item.description_edited === true, "description stored through PATCH", item && item.description);
    // Escape cancels; blur saves.
    await mine.locator("td.workflows-what .workflows-desc-edit").click();
    await mine.locator("td.workflows-what textarea").fill("thrown away");
    await mine.locator("td.workflows-what textarea").press("Escape");
    await page.waitForTimeout(300);
    check(!/thrown away/.test(await mine.locator("td.workflows-what").textContent()), "Escape cancels the edit");
    await mine.locator("td.workflows-what .workflows-desc-edit").click();
    await mine.locator("td.workflows-what textarea").fill("Saved on blur.");
    await page.locator("#workflows-search").click();
    await page.waitForFunction(() => /Saved on blur\./.test(document.querySelector('#workflows-table tr.workflows-row[data-bundle="r8-alice-wf"] td.workflows-what').textContent), null, { timeout: 15000 });
    check(true, "blur saves the description");
    // Older versions: separate rows, each with its own three actions.
    await page.click("#workflows-show-older");
    await page.waitForSelector('tr.workflows-row--older[data-bundle="r8-alice-wf"]');
    const older = page.locator('tr.workflows-row--older[data-bundle="r8-alice-wf"]');
    check(await older.count() === 1, "Older versions on: one older row for r8-alice-wf");
    check((await older.locator("td.workflows-actions button").evaluateAll((bs) => bs.map((b) => b.title))).join("|") === "Export|Open in AbstractFlow|Archive", "older row: its own three icon actions");
    await page.click("#workflows-show-older");
    await page.waitForTimeout(200);
    check(await page.locator("tr.workflows-row--older").count() === 0, "Older versions off: no older rows");
    // One row at every width.
    for (const [w, h] of VIEWPORTS) {
      await page.setViewportSize({ width: w, height: h });
      await page.waitForTimeout(350);
      const tops = await mine.locator("td.workflows-actions button").evaluateAll((bs) => bs.map((b) => Math.round(b.getBoundingClientRect().top)));
      check(new Set(tops).size === 1, `actions never wrap at ${w}px`, tops);
      await noSideScroll(page, `Workflows ${w}px`);
    }
    // The PATCH refuses a shared workflow for alice (the console never offers it).
    const sharedId = await shared.getAttribute("data-bundle");
    const refused = await api(page, "PATCH", `/bundles/${encodeURIComponent(sharedId)}`, { description: "x" });
    check(refused.status === 403 || refused.status === 409, "alice PATCH on a shared workflow refused", refused.status);
    await ctx.close();
  }
  {
    // Admin: shipped workflows have no pencil.
    const { ctx, page } = await newPage(browser, { width: 1440, height: 900 });
    await signIn(page, "admin", ADMIN);
    await workflowsReady(page);
    const shipped = page.locator('#workflows-table tr.workflows-row[data-bundle="basic-agent"]');
    check(await shipped.count() === 1, "admin: basic-agent (shipped) listed");
    check(await shipped.locator(".workflows-desc-edit").count() === 0, "admin: no pencil on a shipped workflow");
    check(await shipped.locator('button[title="Archive"]').count() === 0, "admin: no Archive on a shipped workflow");
    await ctx.close();
  }

  // ------------------------------------------------------------------ no side scroll + screenshots
  for (const theme of ["light", "dark"]) {
    for (const [w, h] of VIEWPORTS) {
      const { ctx, page } = await newPage(browser, { width: w, height: h }, theme);
      await signIn(page, "admin", ADMIN);
      await openTab(page, "apps");
      await page.waitForSelector('[data-app-card="continuum"]', { timeout: 30000 });
      await page.waitForTimeout(300);
      await noSideScroll(page, `Apps ${w}px ${theme}`);
      await shot(page, `apps-${w}-${theme}`);
      await page.click('[data-app-card="continuum"] [data-app-action="settings"]');
      await page.waitForSelector("#app-settings-backdrop:not([hidden]) [data-backlog-settings]");
      await page.waitForTimeout(400);
      await noSideScroll(page, `Continuum settings ${w}px ${theme}`);
      if (SHOTS) { const f = path.join(SHOTS, `continuum-settings-${w}-${theme}.png`); await page.screenshot({ path: f }); shots.push(f); }
      await page.click("#app-settings-close");
      await page.click('#apps-root [data-app-action="apps-settings"]');
      await page.waitForSelector("#app-settings-backdrop:not([hidden]) [data-apps-settings]");
      await page.waitForTimeout(300);
      if (SHOTS) { const f = path.join(SHOTS, `apps-settings-${w}-${theme}.png`); await page.screenshot({ path: f }); shots.push(f); }
      await page.click("#app-settings-close");
      await openTab(page, "skills");
      await page.waitForSelector("#skills-settings-root [data-skills-shelf]", { timeout: 30000 });
      await page.waitForTimeout(300);
      await noSideScroll(page, `Skills ${w}px ${theme}`);
      await shot(page, `skills-${w}-${theme}`);
      await workflowsReady(page);
      await noSideScroll(page, `Workflows (admin) ${w}px ${theme}`);
      await shot(page, `workflows-${w}-${theme}`);
      await openTab(page, "users");
      await page.waitForSelector("#email-cap-recovery");
      await page.waitForTimeout(300);
      if (SHOTS) await shotEl(page, "#email-caps-section", `email-for-everyone-${w}-${theme}`);
      await ctx.close();
    }
  }
  {
    const { ctx, page } = await newPage(browser, { width: 390, height: 844 }, "dark");
    await signIn(page, "alice", ALICE);
    await workflowsReady(page);
    await page.locator('#workflows-table tr.workflows-row[data-bundle="r8-alice-wf"] .workflows-desc-edit').first().click().catch(() => {});
    await page.waitForTimeout(200);
    await shot(page, "workflows-alice-editing-390-dark");
    await ctx.close();
  }
} catch (e) {
  failures.push(`exception: ${e && e.stack ? e.stack : e}`);
} finally {
  await browser.close();
}
console.log(JSON.stringify({ failures, checks, shots }));
