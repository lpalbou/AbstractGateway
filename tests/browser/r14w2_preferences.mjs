// R14.2 browser checks of the Accounts → Preferences modal, run by
// tests/test_r14w2_console_preferences_browser.py against a hermetic scratch gateway.
// A NON-ADMIN (alice) edits her OWN default workflow per app: the row's Preferences icon button
// (kit tooltip "Default workflows of alice"), the modal (title, lead, one row per app, "Gateway
// default (<name>)" first and selected, the executable workflows next), apply on change ("Saved.",
// stored on the gateway), back to Gateway default, and a refusal ("Not saved. <sentence>", the
// select goes back). Optional shots (light/dark) when a shots folder is given.
//
//   node r14w2_preferences.mjs <base-url> <alice-token> <playwright-node-modules> [shots-dir]
//
// Prints one JSON line: {"failures": [...], "checks": N}.
import { createRequire } from "node:module";
import path from "node:path";

const [BASE, TOKEN, PW, SHOTS] = process.argv.slice(2);
const require = createRequire(path.join(PW, "/"));
const { chromium } = require("playwright-core");

const failures = [];
let checks = 0;
function check(ok, what, detail) {
  checks += 1;
  if (!ok) failures.push(detail === undefined ? what : `${what}: ${JSON.stringify(detail)}`);
}

async function open(browser, theme) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 860 }, deviceScaleFactor: 1 });
  await ctx.addInitScript((t) => { try { localStorage.setItem("abstractgateway_ui_settings_v1", JSON.stringify({ theme: t })); } catch {} }, theme);
  const page = await ctx.newPage();
  page.on("pageerror", (e) => failures.push(`pageerror: ${e.message}`));
  await page.goto(`${BASE}/console`);
  await page.waitForSelector("#login-form");
  await page.fill("#login-user", "alice");
  await page.fill("#login-token", TOKEN);
  await page.click("#login-button");
  await page.waitForFunction(() => document.body.classList.contains("signed-in"), null, { timeout: 20000 });
  await page.keyboard.press("Escape").catch(() => {});
  await page.evaluate(() => { document.body.classList.remove("nav-open"); document.getElementById("tab-button-users").click(); });
  await page.waitForSelector("#users-table tr[data-user='alice'] button[data-action='preferences']", { timeout: 15000 });
  return { ctx, page };
}

async function stored(page) {
  return page.evaluate(async () => {
    const r = await fetch("/api/gateway/accounts/me/preferences", { credentials: "same-origin" });
    return (await r.json()).preferences.default_workflow;
  });
}

const CODE = "abstractcode.agent.v1";
const browser = await chromium.launch({ headless: true });
try {
  const { ctx, page } = await open(browser, "light");
  const btn = page.locator("#users-table tr[data-user='alice'] button[data-action='preferences']");
  check((await btn.getAttribute("data-af-tip")) === "Default workflows of alice", "row button carries the kit tooltip sentence", await btn.getAttribute("data-af-tip"));
  check((await btn.getAttribute("aria-label")) === "Preferences of alice", "row button aria-label");
  check(((await btn.textContent()) || "").trim() === "", "icon only, no label");
  await btn.hover();
  await page.waitForTimeout(400);
  const tip = await page.evaluate(() => { const t = document.querySelector(".af-tooltip"); return t && !t.hidden && getComputedStyle(t).visibility !== "hidden" ? t.textContent.trim() : null; });
  check(tip === "Default workflows of alice", "the kit tooltip shows on hover", tip);
  await btn.click();
  await page.waitForSelector("#account-preferences-backdrop:not([hidden]) select[data-account-preference-select]", { timeout: 15000 });
  check((await page.textContent("#account-preferences-title")).trim() === "Preferences — alice", "modal title");
  const lead = (await page.textContent("#account-preferences-body .account-modal-lead")).trim();
  check(lead === "The workflow each app runs for alice unless a conversation picks another. Gateway default follows the admin's Default workflow per app.", "lead sentence", lead);
  const rows = await page.$$eval("[data-account-preference]", (els) => els.map((e) => e.getAttribute("data-account-preference")));
  check(rows.includes(CODE) && rows.includes("abstractassistant.agent.v1"), "one row per app", rows);
  check(await page.locator("#account-preferences-body button", { hasText: /save/i }).count() === 0, "no Save button");
  const sel = page.locator(`select[data-account-preference-select='${CODE}']`);
  const opts = await sel.evaluate((s) => Array.from(s.options).map((o) => ({ v: o.value, t: o.textContent })));
  check(opts[0].v === "" && /^Gateway default \(.+\)$/.test(opts[0].t), "Gateway default (<name>) first", opts[0]);
  check((await sel.inputValue()) === "", "Gateway default selected while nothing is overridden");
  check(opts.length >= 3, "the executable workflows follow", opts.length);
  if (SHOTS) await page.screenshot({ path: path.join(SHOTS, "console-preferences-light.png") });
  // Apply on change.
  const pick = opts.find((o) => o.v && o.v !== "basic-agent:" && !o.t.startsWith("Gateway")) || opts[1];
  await sel.selectOption(pick.v);
  await page.waitForSelector(`[data-account-preference-saved='${CODE}'].ok`, { timeout: 15000 });
  check((await page.textContent(`[data-account-preference-saved='${CODE}']`)).trim() === "Saved.", "Saved.");
  let s = await stored(page);
  check(s[CODE] === pick.v, "stored on the gateway", s);
  check((await page.locator(`select[data-account-preference-select='${CODE}']`).inputValue()) === pick.v, "the select shows the override");
  // Reopen: the gateway's answer, not a client copy.
  await page.click("#account-preferences-close");
  await btn.click();
  await page.waitForSelector(`#account-preferences-backdrop:not([hidden]) select[data-account-preference-select='${CODE}']`);
  check((await page.locator(`select[data-account-preference-select='${CODE}']`).inputValue()) === pick.v, "reopened: the override is selected");
  // Refusal: the gateway's sentence + "Not saved.", the select goes back.
  await page.route("**/api/gateway/accounts/me/preferences", async (route) => {
    if (route.request().method() === "PUT") {
      await route.fulfill({ status: 400, contentType: "application/json", body: JSON.stringify({ detail: { reason: "preference_refused", message: "default_workflow.abstractcode.agent.v1 = 'x:y' refused: workflow bundle 'x' is not on this gateway.", key: "default_workflow" } }) });
    } else await route.continue();
  });
  await page.locator(`select[data-account-preference-select='${CODE}']`).selectOption("");
  await page.waitForSelector(`[data-account-preference-saved='${CODE}'].error`, { timeout: 15000 });
  const refusal = (await page.textContent(`[data-account-preference-saved='${CODE}']`)).trim();
  check(refusal === "Not saved. default_workflow.abstractcode.agent.v1 = 'x:y' refused: workflow bundle 'x' is not on this gateway.", "Not saved. + the gateway sentence", refusal);
  check((await page.locator(`select[data-account-preference-select='${CODE}']`).inputValue()) === pick.v, "the select goes back to the stored value");
  await page.unroute("**/api/gateway/accounts/me/preferences");
  // Back to Gateway default.
  await page.locator(`select[data-account-preference-select='${CODE}']`).selectOption("");
  await page.waitForSelector(`[data-account-preference-saved='${CODE}'].ok`, { timeout: 15000 });
  s = await stored(page);
  check(s[CODE] === null, "Gateway default stores null", s);
  await ctx.close();
  if (SHOTS) {
    const dark = await open(browser, "dark");
    await dark.page.locator("#users-table tr[data-user='alice'] button[data-action='preferences']").click();
    await dark.page.waitForSelector("#account-preferences-backdrop:not([hidden]) select[data-account-preference-select]");
    await dark.page.screenshot({ path: path.join(SHOTS, "console-preferences-dark.png") });
    await dark.ctx.close();
  }
} finally {
  await browser.close();
}
console.log(JSON.stringify({ failures, checks }));
