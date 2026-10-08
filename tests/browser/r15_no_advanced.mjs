// R15 D1 (operator: no "Advanced" disclosures anywhere) browser checks, run by
// tests/test_r15_console_no_advanced_browser.py against a hermetic scratch gateway (admin).
// Every surface that used to hide fields behind "Advanced" shows them in a visible section
// named for its content (the TUI lead's "R15 SECTION NAMES", verbatim):
//   Email (account Email modal): "Recipients and limits", "Sign-in app"
//   Create user: "Runtime and tenant"; Configure provider: "Visible models"
//   Create entity: "Optional configuration"
//   Network: Allowed origins + Trust proxies inside "Reached through another address?"
//   Workflows: the streaming CLI line is a plain sub-line; other workflow types are a named group.
// And, in the live DOM after each surface opened: no <summary> says "Advanced", no visible
// text says "Advanced".
//
//   node r15_no_advanced.mjs <base-url> <admin-token> <playwright-node-modules> [shots-dir]
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
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 1600 }, deviceScaleFactor: 1, colorScheme: theme });
  await ctx.addInitScript((t) => { try { localStorage.setItem("abstractgateway_ui_settings_v1", JSON.stringify({ theme: t })); } catch {} }, theme);
  const page = await ctx.newPage();
  page.on("pageerror", (e) => failures.push(`pageerror@${theme}: ${e.message}`));
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
const noAdvanced = (page, where, theme) => page.evaluate(() => ({
  summaries: [...document.querySelectorAll("summary")].map((s) => s.textContent.trim()).filter((t) => /advanced/i.test(t)),
  visible: [...document.querySelectorAll("body *")].filter((el) => el.childElementCount === 0 && /\bAdvanced\b/.test(el.textContent || "") && el.checkVisibility && el.checkVisibility()).map((el) => el.textContent.trim().slice(0, 60)),
})).then((r) => {
  check(r.summaries.length === 0, `${theme} ${where}: no <summary> says Advanced`, r.summaries);
  check(r.visible.length === 0, `${theme} ${where}: no visible "Advanced"`, r.visible);
});
const visibleTitle = (page, sel, text) => page.evaluate(([s, t]) => { const el = document.querySelector(s); return !!el && el.textContent.trim() === t && el.checkVisibility() && !el.closest("details"); }, [sel, text]);
async function shot(page, name, theme) { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `r15-${name}-${theme}.png`), fullPage: true }); }

const browser = await chromium.launch({ headless: true });
try {
  for (const theme of ["light", "dark"]) {
    const { ctx, page } = await open(browser, theme);
    // Accounts → Email modal (the account page, moved into the modal).
    await tab(page, "tab-button-users");
    await page.waitForSelector("#users-table tr[data-user='admin'] button[data-action='email']", { timeout: 15000 });
    await page.click("#users-table tr[data-user='admin'] button[data-action='email']");
    await page.waitForFunction(() => !document.getElementById("account-email-backdrop").hidden, null, { timeout: 15000 });
    await page.waitForTimeout(800);
    check(await visibleTitle(page, "#my-email-limits-title", "Recipients and limits"), `${theme} Email: "Recipients and limits" visible`);
    check(await page.evaluate(() => document.getElementById("my-email-imap-folder").checkVisibility() && document.getElementById("my-email-per-hour").checkVisibility()), `${theme} Email: limits and watch folder visible`);
    // The Sign-in app section belongs to the Google/Microsoft tabs.
    await page.click("[data-email-tab='google']").catch(() => {});
    await page.waitForTimeout(300);
    check(await visibleTitle(page, "#my-email-oauth-app-title", "Sign-in app"), `${theme} Email: "Sign-in app" visible`);
    check(await page.evaluate(() => document.getElementById("my-email-oauth-client-id").checkVisibility()), `${theme} Email: Client ID visible`);
    await noAdvanced(page, "Email modal", theme);
    await shot(page, "email", theme);
    await page.keyboard.press("Escape");
    await page.waitForTimeout(300);
    // Create user.
    await page.click("#open-create-user");
    await page.waitForTimeout(400);
    check(await visibleTitle(page, "#new-user-runtime-tenant-title", "Runtime and tenant"), `${theme} Create user: "Runtime and tenant" visible`);
    check(await page.evaluate(() => document.getElementById("new-runtime").checkVisibility() && document.getElementById("new-tenant").checkVisibility()), `${theme} Create user: Runtime and Tenant fields visible`);
    await noAdvanced(page, "Create user", theme);
    await shot(page, "create-user", theme);
    await page.keyboard.press("Escape");
    await page.waitForTimeout(300);
    // Create entity.
    await page.click("#accounts-create-entity");
    await page.waitForTimeout(800);
    check(await visibleTitle(page, "#entity-optional-title", "Optional configuration"), `${theme} Create entity: "Optional configuration" visible`);
    check(await page.evaluate(() => document.getElementById("entity-new-provider").checkVisibility()), `${theme} Create entity: substrate fields visible`);
    await noAdvanced(page, "Create entity", theme);
    await shot(page, "create-entity", theme);
    await page.keyboard.press("Escape");
    await page.waitForTimeout(300);
    // Providers → configure a provider.
    await tab(page, "tab-button-providers");
    await page.waitForTimeout(800);
    await page.evaluate(() => openEndpointModalForFamily("openai"));
    await page.waitForTimeout(400);
    check(await visibleTitle(page, "#endpoint-visible-models-title", "Visible models"), `${theme} Configure provider: "Visible models" visible`);
    check(await page.evaluate(() => document.getElementById("endpoint-models").checkVisibility()), `${theme} Configure provider: model list visible`);
    await noAdvanced(page, "Configure provider", theme);
    await shot(page, "provider", theme);
    await page.keyboard.press("Escape");
    await page.evaluate(() => document.getElementById("provider-modal-backdrop").classList.add("hidden"));
    // Network.
    await tab(page, "tab-button-network");
    await page.waitForSelector("[data-net-other]", { timeout: 15000 });
    await page.waitForTimeout(500);
    const net = await page.evaluate(() => { const sec = document.querySelector("[data-net-other]"); const o = document.getElementById("net-origins-h"); const t = document.querySelector("[data-net-trust]"); return { originsInCard: !!o && sec.contains(o) && o.checkVisibility(), trustInCard: !!t && sec.contains(t), details: !!document.querySelector("[data-net-proxy]")?.closest("details") }; });
    check(net.originsInCard && net.trustInCard && !net.details, `${theme} Network: origins and proxy trust shown inside "Reached through another address?"`, net);
    await noAdvanced(page, "Network", theme);
    await shot(page, "network", theme);
    // Workflows: streaming CLI line visible as a plain sub-line; no disclosure in the defaults.
    await tab(page, "tab-button-workflows");
    await page.waitForTimeout(1500);
    const wf = await page.evaluate(() => { const cli = document.querySelector("[data-streaming-default-cli]"); const d = document.querySelector("[data-agent-defaults]"); return { cli: !!cli && cli.checkVisibility(), details: d ? d.querySelectorAll("details:not(.help-q)").length : -1 }; });  // a (?) help bubble is not a disclosure of settings
    check(wf.cli, `${theme} Workflows: the streaming CLI line is a visible sub-line`, wf);
    check(wf.details === 0, `${theme} Workflows: no settings disclosure in the default workflows`, wf);
    await noAdvanced(page, "Workflows", theme);
    await shot(page, "workflows", theme);
    await ctx.close();
  }
} finally {
  await browser.close();
}
console.log(JSON.stringify({ failures, checks }));
