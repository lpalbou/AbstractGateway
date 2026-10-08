// R16.1 browser checks of the Accounts → Preferences time-zone row, run by
// tests/test_r16w2_console_time_zone_browser.py against a hermetic scratch gateway.
// A NON-ADMIN (alice) edits her OWN time zone: the kit AfTimeZonePicker island (label and help
// from the gateway's `time_zone` block, "Gateway default (<zone>)" first and shown while nothing
// is stored), search over the SERVED IANA names, apply on pick ("Saved.", stored on the gateway),
// back to Gateway default (stored null), a refusal ("Not saved. <sentence>"), the island
// unmounted when the modal closes, and a missing time_zone block failing loud. Optional shots
// (light/dark at 1440/834/390, picker open) when a shots folder is given.
//
//   node r16w2_time_zone.mjs <base-url> <alice-token> <playwright-node-modules> [shots-dir]
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

async function open(browser, theme, width = 1280, height = 860) {
  const ctx = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 1 });
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

async function prefs(page) {
  return page.evaluate(async () => {
    const r = await fetch("/api/gateway/accounts/me/preferences", { credentials: "same-origin" });
    return r.json();
  });
}

async function openModal(page) {
  await page.locator("#users-table tr[data-user='alice'] button[data-action='preferences']").click();
  await page.waitForSelector("#account-preferences-backdrop:not([hidden]) [data-account-preference='time_zone'] .af-tz-picker", { timeout: 15000 });
}

const TZ = "[data-account-preference='time_zone']";
const browser = await chromium.launch({ headless: true });
try {
  const { ctx, page } = await open(browser, "light");
  const before = await prefs(page);
  const block = before.time_zone;
  check(block && Array.isArray(block.choices) && block.choices.includes("Europe/Paris") && typeof block.gateway_default === "string", "the gateway serves the time_zone block", block && Object.keys(block));
  check(before.preferences && before.preferences.time_zone === null, "nothing stored at first", before.preferences);
  await openModal(page);
  const rows = await page.$$eval("#account-preferences-body [data-account-preference]", (els) => els.map((e) => e.getAttribute("data-account-preference")));
  check(rows[rows.length - 1] === "time_zone", "the time-zone row comes after the default-workflow rows", rows);
  check((await page.textContent(`${TZ} .af-tz-picker__label`)).trim() === block.label, "label = the gateway's", block.label);
  check((await page.getAttribute(`${TZ} .af-tz-picker__help`, "data-af-tip")) === block.help, "help = the gateway's sentence, as a kit tooltip");
  const trigger = page.locator(`${TZ} .af-select-trigger`);
  const def = `Gateway default (${block.gateway_default})`;
  check(((await trigger.textContent()) || "").replace("▾", "").trim() === def, "the trigger shows Gateway default (<zone>) while nothing is stored", await trigger.textContent());
  check(await page.locator("#account-preferences-body button", { hasText: /^\s*save\s*$/i }).count() === 0, "no Save button");
  // Open, search the served names, pick.
  await trigger.click();
  await page.waitForSelector(".af-select-popover .af-select-search-input");
  const first = (await page.textContent(".af-select-popover [role='option']")).trim();
  check(first === def, "first option = Gateway default (<zone>)", first);
  const total = await page.locator(".af-select-popover [role='option']").count();
  check(total === block.choices.length + 1, "the options are exactly the served choices + the default", { total, served: block.choices.length });
  await page.fill(".af-select-popover .af-select-search-input", "Europe/Par");
  const filtered = await page.$$eval(".af-select-popover [role='option']", (els) => els.map((e) => e.textContent.trim()));
  check(filtered.includes("Europe/Paris") && filtered.every((t) => t.includes("Europe/Par")), "search filters the served names", filtered.slice(0, 5));
  if (SHOTS) await page.screenshot({ path: path.join(SHOTS, "web-picker-open-search-light-1280.png") });
  await page.locator(".af-select-popover [role='option']", { hasText: /^Europe\/Paris$/ }).click();
  await page.waitForSelector(`${TZ} .af-tz-picker__note.is-ok`, { timeout: 15000 });
  check((await page.textContent(`${TZ} .af-tz-picker__note`)).trim() === "Saved.", "Saved.");
  const after = await prefs(page);
  check(after.preferences.time_zone === "Europe/Paris" && after.time_zone.effective === "Europe/Paris", "stored on the gateway", after.preferences.time_zone);
  check(((await page.locator(`${TZ} .af-select-trigger`).textContent()) || "").includes("Europe/Paris"), "the trigger shows the stored zone");
  // Back to the gateway default: null, never the label.
  await page.locator(`${TZ} .af-select-trigger`).click();
  await page.waitForSelector(".af-select-popover [role='option']");
  await page.locator(".af-select-popover [role='option']").first().click();
  await page.waitForSelector(`${TZ} .af-tz-picker__note.is-ok`, { timeout: 15000 });
  const back = await prefs(page);
  check(back.preferences.time_zone === null && back.time_zone.value === null && back.time_zone.effective === block.gateway_default, "Gateway default stores null", back.preferences.time_zone);
  // A refusal (the gateway's sentence), the stored value unchanged.
  await page.route("**/api/gateway/accounts/me/preferences", async (route) => {
    if (route.request().method() !== "PUT") return route.continue();
    await route.fulfill({ status: 400, contentType: "application/json", body: JSON.stringify({ detail: { reason: "preference_refused", message: "time_zone = 'Mars/Olympus' refused: use an IANA time zone name such as 'Europe/Paris', or null for the gateway default.", key: "time_zone" } }) });
  });
  await page.locator(`${TZ} .af-select-trigger`).click();
  await page.fill(".af-select-popover .af-select-search-input", "Asia/Tokyo");
  await page.locator(".af-select-popover [role='option']", { hasText: /^Asia\/Tokyo$/ }).click();
  await page.waitForSelector(`${TZ} .af-tz-picker__note.is-error`, { timeout: 15000 });
  const refused = (await page.textContent(`${TZ} .af-tz-picker__note`)).trim();
  check(refused.startsWith("Not saved. ") && refused.includes("refused"), "Not saved. <sentence>", refused);
  await page.unroute("**/api/gateway/accounts/me/preferences");
  check((await prefs(page)).preferences.time_zone === null, "a refusal stores nothing");
  // Closing unmounts the island.
  await page.click("#account-preferences-close");
  await page.waitForSelector("#account-preferences-backdrop[hidden]", { state: "attached" });
  check(await page.locator(".af-tz-picker").count() === 0, "the island is unmounted when the modal closes");
  // A gateway without the time_zone block: the modal fails loud (no silent hole).
  await page.route("**/api/gateway/accounts/me/preferences", async (route) => {
    const res = await route.fetch();
    const body = await res.json();
    delete body.time_zone;
    await route.fulfill({ response: res, body: JSON.stringify(body) });
  });
  await page.locator("#users-table tr[data-user='alice'] button[data-action='preferences']").click();
  await page.waitForSelector("#account-preferences-body .ui-alert.tone-err", { timeout: 15000 });
  const loud = (await page.textContent("#account-preferences-body .ui-alert")).trim();
  check(loud.includes("time_zone") && loud.includes("R16.1 preferences seam"), "a missing time_zone block fails loud", loud);
  await page.unroute("**/api/gateway/accounts/me/preferences");
  await page.click("#account-preferences-close");
  await ctx.close();

  if (SHOTS) {
    for (const theme of ["light", "dark"]) {
      for (const [w, h] of [[1440, 900], [834, 1112], [390, 844]]) {
        const o = await open(browser, theme, w, h);
        await openModal(o.page);
        await o.page.locator(`${TZ} .af-select-trigger`).scrollIntoViewIfNeeded();
        await o.page.screenshot({ path: path.join(SHOTS, `web-preferences-${theme}-${w}.png`) });
        await o.page.locator(`${TZ} .af-select-trigger`).click();
        await o.page.waitForSelector(".af-select-popover [role='option']");
        await o.page.screenshot({ path: path.join(SHOTS, `web-preferences-picker-open-${theme}-${w}.png`) });
        const overflow = await o.page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
        check(!overflow, `no horizontal page scroll at ${w} (${theme})`);
        await o.ctx.close();
      }
    }
  }
} catch (e) {
  failures.push(`exception: ${e && e.stack ? e.stack : e}`);
} finally {
  await browser.close();
}
console.log(JSON.stringify({ failures, checks }));
process.exit(failures.length ? 1 : 0);
