// R16.5 in a real browser: the creator configures their entity (operator ruling 2026-10-08).
//
//   node r16w6_creator.mjs <base-url> <tokens.json> <playwright-node-modules> [shots-dir]
//
// The gateway holds alice (member) who created Nova, bob (member) and an admin; an endpoint
// profile "endpoint:demo" offers demo-small / demo-large (static list, nothing probed).
// Prints one JSON line: {"failures": [...], "checks": N}.
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";

const [BASE, TOKENS, PW, SHOTS] = process.argv.slice(2);
if (!PW) throw new Error("r16w6_creator.mjs needs <base> <tokens.json> <playwright node_modules> [shots]");
const require = createRequire(path.join(PW, "/"));
const { chromium } = require("playwright-core");
const tokens = JSON.parse(fs.readFileSync(TOKENS, "utf8"));
const ENTITY = "nova";

const failures = [];
let checks = 0;
let step = "start";
function check(ok, what, detail) {
  checks += 1;
  step = what;
  if (!ok) failures.push(detail === undefined ? what : `${what}: ${JSON.stringify(detail)}`);
}

async function open(browser, who, theme, viewport = { width: 1280, height: 860 }) {
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: 1 });
  await ctx.addInitScript((t) => { try { localStorage.setItem("abstractgateway_ui_settings_v1", JSON.stringify({ theme: t })); } catch {} }, theme);
  const page = await ctx.newPage();
  page.on("pageerror", (e) => failures.push(`pageerror(${who}): ${e.message}`));
  await page.goto(`${BASE}/console`);
  await page.waitForSelector("#login-form");
  await page.fill("#login-user", who);
  await page.fill("#login-token", tokens[who]);
  await page.click("#login-button");
  await page.waitForFunction(() => document.body.classList.contains("signed-in"), null, { timeout: 30000 });
  await page.keyboard.press("Escape").catch(() => {});
  await page.evaluate(() => { document.body.classList.remove("nav-open"); document.getElementById("tab-button-users").click(); });
  await page.waitForSelector(`#users-table tr[data-user='${who === "admin" ? ENTITY : who}']`, { timeout: 20000 });
  return { ctx, page };
}

async function shot(page, name) {
  if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `${name}.png`) });
}

async function tipOf(page, locator) {
  await locator.scrollIntoViewIfNeeded();
  await page.waitForTimeout(300);
  const box = await locator.boundingBox();
  await page.mouse.move(box.x + 2, box.y + 2);
  await page.waitForTimeout(100);
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.waitForTimeout(600);
  return page.evaluate(() => { const t = document.querySelector(".af-tooltip"); return t && !t.hidden && getComputedStyle(t).visibility !== "hidden" ? t.textContent.trim() : null; });
}

async function apiAs(page, method, url, body) {
  return page.evaluate(async ({ method, url, body }) => {
    const csrf = (document.cookie.split("; ").find((c) => c.startsWith("abstractgateway_csrf=")) || "").split("=")[1] || "";
    const r = await fetch(url, { method, credentials: "same-origin", headers: { "Content-Type": "application/json", "X-AbstractGateway-CSRF": decodeURIComponent(csrf) }, body: body ? JSON.stringify(body) : undefined });
    return { status: r.status, body: await r.json().catch(() => null) };
  }, { method, url, body });
}

const browser = await chromium.launch({ headless: true });
try {
  for (const theme of ["light", "dark"]) {
    const { ctx, page } = await open(browser, "alice", theme);
    const row = page.locator(`#users-table tr[data-user='${ENTITY}']`);
    // The creator's entity row: Manage + Archive offered, the Active switch usable.
    check(await row.locator("button[data-action='manage']").count() === 1, `${theme}: Manage offered to the creator`);
    check(await row.locator("button[data-action='archive']").count() === 1, `${theme}: Archive offered to the creator`);
    const active = row.locator("button[role='switch']");
    check((await active.getAttribute("aria-disabled")) !== "true", `${theme}: the Active switch is usable by the creator`, await active.getAttribute("aria-disabled"));
    const chip = (await page.locator(`#users-table tr[data-user='alice'] .af-kind-chip`).textContent()).trim();
    check(chip === "Member", `${theme}: a human non-admin is a Member`, chip);
    if (theme === "light") await shot(page, `r16w6-accounts-creator-${theme}`);
    // Manage: settings editable, lifecycle acts hidden.
    await row.locator("button[data-action='manage']").click();
    await page.waitForSelector("#entity-manage-backdrop:not([hidden])");
    await page.waitForFunction(() => !document.getElementById("entity-manage-section").classList.contains("entity-manage--noconfig"), null, { timeout: 15000 });
    check(await page.locator("#entity-manage-section.entity-manage--readonly").count() === 1, `${theme}: lifecycle acts stay an admin's (readonly class)`);
    await page.click("#entity-subtab-substrate");
    const pickersUp = await page.waitForFunction(() => ["entity-mind-picker", "entity-voice-picker"].every((id) => { const el = document.getElementById(id); return el && el.children.length > 0 && el.getClientRects().length > 0; }), null, { timeout: 15000 }).then(() => true, () => false);
    check(pickersUp && await page.locator("#entity-mind-picker").isVisible(), `${theme}: the creator sees the mind picker`);
    check(pickersUp && await page.locator("#entity-voice-picker").isVisible(), `${theme}: the creator sees the voice picker`);
    check((await page.textContent("#entity-substrate-current")).trim() === "", `${theme}: no read-only mind line for the creator`);
    check(!(await page.locator("#entity-reembed-box").isVisible()), `${theme}: the memory index rebuild stays hidden`);
    await page.waitForTimeout(800);
    await shot(page, `r16w6-manage-mind-creator-${theme}`);
    await page.click("#entity-subtab-tools");
    await page.waitForSelector("#entity-manage-matrix input[type=checkbox]");
    const cells = await page.$$eval("#entity-manage-matrix input[type=checkbox]", (els) => els.map((e) => ({ tool: e.dataset.tool, phase: e.dataset.phase, disabled: e.disabled, checked: e.checked, tip: e.parentElement.getAttribute("data-af-tip") })));
    const exec = cells.filter((c) => c.tool === "execute_command" && !c.checked);
    check(exec.length > 0 && exec.every((c) => c.disabled && c.tip === "Only an admin can give nova a tier-2 tool: it acts on the world outside its memory and workspace."), `${theme}: execute_command can't be given by the creator (served sentence)`, exec);
    check(cells.filter((c) => c.tool !== "execute_command").every((c) => !c.disabled), `${theme}: every other box is the creator's to tick`);
    check(await page.locator("#entity-tools-denyall-row").isVisible(), `${theme}: the creator sees Empty phase means no tools`);
    if (theme === "light") {
      const tip = await tipOf(page, page.locator("#entity-manage-matrix input[data-tool='execute_command'][data-phase='visit']").locator(".."));
      check(tip === "Only an admin can give nova a tier-2 tool: it acts on the world outside its memory and workspace.", "hovering the refused cell shows the kit tooltip", tip);
    }
    await shot(page, `r16w6-manage-tools-creator-${theme}`);
    await page.click("#entity-subtab-prompt");
    await page.waitForSelector("#entity-prompt-layers textarea");
    check(await page.$$eval("#entity-prompt-layers textarea", (els) => els.length > 0 && els.every((t) => !t.readOnly)), `${theme}: the creator edits its instructions`);
    await page.click("#entity-subtab-lifecycle");
    check(!(await page.locator("#entity-state-awake").isVisible()), `${theme}: sleep/wake stays an admin's`);
    check(!(await page.locator("#entity-loop-freeze").isVisible()), `${theme}: freeze stays an admin's`);
    await shot(page, `r16w6-manage-lifecycle-creator-${theme}`);
    await page.click("#entity-manage-close");
    await ctx.close();
  }

  // A creator write through the console's own session: tick a tool box; the save lands.
  {
    const { ctx, page } = await open(browser, "alice", "light");
    // Start from an active Nova (a previous run may have left it suspended).
    const reset = await apiAs(page, "PUT", `/api/gateway/me/accounts/${ENTITY}/active`, { active: true });
    check(reset.status === 200, "the creator can turn Nova on through /me", reset.status);
    await page.evaluate(() => document.getElementById("tab-button-users").click());
    await page.waitForFunction((id) => { const b = document.querySelector(`#users-table tr[data-user='${id}'] button[role='switch']`); return b && b.getAttribute("aria-checked") === "true"; }, ENTITY, { timeout: 15000 });
    await page.locator(`#users-table tr[data-user='${ENTITY}'] button[data-action='manage']`).click();
    await page.waitForFunction(() => !document.getElementById("entity-manage-section").classList.contains("entity-manage--noconfig"), null, { timeout: 15000 });
    await page.click("#entity-subtab-tools");
    const box = page.locator("#entity-manage-matrix input[data-tool='fetch_url'][data-phase='sleep']");
    const was = await box.isChecked();
    await box.click();
    await page.waitForFunction(() => /Saved/.test(document.getElementById("entity-tools-out").textContent || ""), null, { timeout: 15000 }).catch(() => {});
    const out = (await page.textContent("#entity-tools-out")).trim();
    check(/^Saved/.test(out), "the creator's tool tick saves", out);
    const tp = await apiAs(page, "GET", `/api/gateway/entities/${ENTITY}/tool-policy`);
    check(tp.status === 200 && tp.body.phases.sleep.tools.includes("fetch_url") !== was, "the gateway holds the creator's change", tp.body && tp.body.phases && tp.body.phases.sleep);
    // The mind: a model the gateway offers saves; the refusal names the offered set.
    const bad = await apiAs(page, "PUT", `/api/gateway/entities/${ENTITY}/substrate`, { provider: "endpoint:demo", model: "not-offered" });
    check(bad.status === 403 && bad.body.detail.message === "not-offered isn't a endpoint:demo model this gateway offers. Offered: demo-large, demo-small.", "an unoffered model is refused with the offered set", bad);
    const ok = await apiAs(page, "PUT", `/api/gateway/entities/${ENTITY}/substrate`, { provider: "endpoint:demo", model: "demo-large" });
    check(ok.status === 200 && ok.body.model === "demo-large", "an offered model saves for the creator", ok.status);
    await page.click("#entity-manage-close");

    // Active off (inline confirm) and on, then Archive → Show archived → Unarchive.
    const row = () => page.locator(`#users-table tr[data-user='${ENTITY}']`);
    await row().locator("button[role='switch']").click();
    await page.waitForSelector("#users-table .row-confirm button.danger");
    await page.click("#users-table .row-confirm button.danger");
    await page.waitForFunction((id) => { const b = document.querySelector(`#users-table tr[data-user='${id}'] button[role='switch']`); return b && b.getAttribute("aria-checked") === "false"; }, ENTITY, { timeout: 15000 });
    check(true, "the creator suspends Nova");
    await row().locator("button[role='switch']").click();
    await page.waitForFunction((id) => { const b = document.querySelector(`#users-table tr[data-user='${id}'] button[role='switch']`); return b && b.getAttribute("aria-checked") === "true"; }, ENTITY, { timeout: 15000 });
    check(true, "the creator resumes Nova");
    await row().locator("button[data-action='archive']").click();
    await page.click("#users-table .row-confirm button.danger");
    await page.waitForFunction((id) => !document.querySelector(`#users-table tr[data-user='${id}']`), ENTITY, { timeout: 15000 });
    const archivedSwitch = page.locator("#accounts-show-archived");
    check(await archivedSwitch.isVisible(), "Show archived is offered to a member");
    await archivedSwitch.click();
    await page.waitForSelector(`#users-table tr[data-user='${ENTITY}'][data-archived='true'] button[data-action='unarchive']`, { timeout: 15000 });
    await shot(page, "r16w6-accounts-archived-creator-light");
    await page.click(`#users-table tr[data-user='${ENTITY}'] button[data-action='unarchive']`);
    await page.waitForFunction((id) => { const tr = document.querySelector(`#users-table tr[data-user='${id}']`); return tr && !tr.hasAttribute("data-archived"); }, ENTITY, { timeout: 15000 });
    const msg = (await page.textContent("#users-message")).trim();
    check(msg === "nova is back, inactive: turn Active on to let it act.", "unarchive says it is back inactive", msg);
    await archivedSwitch.click();
    await row().locator("button[role='switch']").click();
    await page.waitForFunction((id) => { const b = document.querySelector(`#users-table tr[data-user='${id}'] button[role='switch']`); return b && b.getAttribute("aria-checked") === "true"; }, ENTITY, { timeout: 15000 });
    check(true, "the creator turns it back on after unarchiving");
    await ctx.close();
  }

  // bob never sees Nova; an admin sees everything and every control.
  {
    const { ctx, page } = await open(browser, "bob", "light");
    check(await page.locator(`#users-table tr[data-user='${ENTITY}']`).count() === 0, "another member never sees Nova");
    const r = await apiAs(page, "PUT", `/api/gateway/entities/${ENTITY}/prompt`, { overlay: { operator: "x" } });
    check(r.status === 404, "another member's write answers like a missing entity", r.status);
    await ctx.close();
  }
  // "Admins always" (R16.5 F1): an admin manages Nova although it lives in alice's runtime plane.
  {
    const { ctx, page } = await open(browser, "admin", "light");
    await page.waitForSelector(`#users-table tr[data-user='${ENTITY}'] button[data-action='manage']`, { timeout: 20000 });
    await page.locator(`#users-table tr[data-user='${ENTITY}'] button[data-action='manage']`).click();
    await page.waitForFunction(() => !document.getElementById("entity-manage-section").classList.contains("entity-manage--noconfig"), null, { timeout: 15000 });
    check(await page.locator("#entity-manage-section.entity-manage--readonly").count() === 0, "an admin has every control on a member's entity");
    const acc = await apiAs(page, "GET", `/api/gateway/entities/${ENTITY}/access`);
    check(acc.status === 200 && acc.body.as === "admin", "the admin's access to a member's entity", acc);
    await page.click("#entity-subtab-substrate");
    const up = await page.waitForFunction(() => { const el = document.getElementById("entity-mind-picker"); return el && el.children.length > 0; }, null, { timeout: 15000 }).then(() => true, () => false);
    check(up, "the admin sees Nova's mind picker");
    await shot(page, "r16w6-manage-mind-admin-on-member-entity-light");
    await ctx.close();
  }
  // An admin manages the entities of its own runtime (Vega, created by the admin): every control.
  for (const theme of ["light", "dark"]) {
    const { ctx, page } = await open(browser, "admin", theme);
    await page.waitForSelector("#users-table tr[data-user='vega'] button[data-action='manage']", { timeout: 20000 });
    await page.locator("#users-table tr[data-user='vega'] button[data-action='manage']").click();
    await page.waitForFunction(() => !document.getElementById("entity-manage-section").classList.contains("entity-manage--noconfig"), null, { timeout: 15000 });
    check(await page.locator("#entity-manage-section.entity-manage--readonly").count() === 0, `${theme}: an admin has every control`);
    await page.click("#entity-subtab-tools");
    await page.waitForSelector("#entity-manage-matrix input[type=checkbox]");
    check(await page.$$eval("#entity-manage-matrix input[type=checkbox]", (els) => els.every((e) => !e.disabled)), `${theme}: an admin may give execute_command`);
    await shot(page, `r16w6-manage-tools-admin-${theme}`);
    await ctx.close();
  }
} catch (e) {
  failures.push(`exception after "${step}": ${e && e.stack ? e.stack.split("\n").slice(0, 3).join(" | ") : e}`);
} finally {
  await browser.close();
}
console.log(JSON.stringify({ failures, checks }));
