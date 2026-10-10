// R18 browser checks of the Spoken language preference, run by
// tests/test_r18_console_spoken_language_browser.py against a hermetic scratch gateway.
//  - Accounts → Preferences (alice, a NON-ADMIN, her own account): the row after the time zone,
//    label/help/options verbatim from the gateway's `spoken_language` block, no Save, a change is
//    ONE PUT {spoken_language} ("Saved."), the stored value selected after a reload, back to Auto,
//    a refusal from the REAL gateway ("Not saved. <its sentence>"), a missing block failing loud.
//  - Multimodal (the admin, his own account): the line under the capability table, before the
//    page message, same wording/choices, PUT on change, "Saved.", the stored value after a reload,
//    a refusal; alice's preference untouched by the admin's.
//  - No horizontal page scroll at 1440/834/390 in light and dark; optional shots.
//
//   node r18_spoken_language.mjs <base-url> <alice-token> <admin-token> <playwright-node-modules> [shots-dir]
//
// Prints one JSON line: {"failures": [...], "checks": N}.
import { createRequire } from "node:module";
import path from "node:path";

const [BASE, ALICE, ADMIN, PW, SHOTS] = process.argv.slice(2);
const require = createRequire(path.join(PW, "/"));
const { chromium } = require("playwright-core");

const failures = [];
let checks = 0;
function check(ok, what, detail) {
  checks += 1;
  if (!ok) failures.push(detail === undefined ? what : `${what}: ${JSON.stringify(detail)}`);
}

async function signIn(browser, who, token, theme, width = 1280, height = 900) {
  const ctx = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 1, colorScheme: theme });
  await ctx.addInitScript((t) => { try { localStorage.setItem("abstractgateway_ui_settings_v1", JSON.stringify({ theme: t })); } catch {} }, theme);
  const page = await ctx.newPage();
  page.on("pageerror", (e) => failures.push(`pageerror: ${e.message}`));
  await page.goto(`${BASE}/console`);
  await login(page, who, token);
  return { ctx, page };
}

async function login(page, who, token) {
  await page.waitForSelector("#login-form");
  await page.fill("#login-user", who);
  await page.fill("#login-token", token);
  await page.click("#login-button");
  await page.waitForFunction(() => document.body.classList.contains("signed-in"), null, { timeout: 20000 });
  await page.keyboard.press("Escape").catch(() => {});
}

// A reload keeps the session cookie: sign in again only when the page asks.
async function reload(page, who, token) {
  await page.reload();
  const kept = await page.waitForFunction(() => document.body.classList.contains("signed-in"), null, { timeout: 10000 }).then(() => true, () => false);
  if (!kept) await login(page, who, token);
  await page.keyboard.press("Escape").catch(() => {});
}

async function toUsers(page) {
  await page.evaluate(() => { document.body.classList.remove("nav-open"); document.getElementById("tab-button-users").click(); });
  await page.waitForSelector("#users-table tr[data-user='alice'] button[data-action='preferences']", { timeout: 15000 });
}

async function toMultimodal(page) {
  await page.evaluate(() => { document.body.classList.remove("nav-open"); document.getElementById("tab-button-defaults").click(); });
  await page.waitForSelector("#defaults-spoken-language-line:not([hidden]) select#defaults-spoken-language", { timeout: 20000 });
}

async function prefs(page) {
  return page.evaluate(async () => {
    const r = await fetch("/api/gateway/accounts/me/preferences", { credentials: "same-origin" });
    return r.json();
  });
}

// Every PUT body the page sends to the preferences route.
function recordPuts(page) {
  const puts = [];
  page.on("request", (req) => {
    if (req.method() === "PUT" && req.url().includes("/preferences")) puts.push(req.postData());
  });
  return puts;
}

// The page sends a REAL value; the route swaps it for an unknown code so the gateway's own
// refusal sentence comes back.
// The gateway's refusals seen by forceUnknownCode: {status, message}.
const refusals = [];
async function forceUnknownCode(page) {
  await page.route("**/api/gateway/accounts/me/preferences", async (route) => {
    if (route.request().method() !== "PUT") return route.continue();
    const res = await route.fetch({ postData: JSON.stringify({ spoken_language: "xx" }) });
    const text = await res.text();
    let message = null;
    try { const d = JSON.parse(text).detail; message = d && typeof d === "object" ? d.message : null; } catch {}
    refusals.push({ status: res.status(), message });
    await route.fulfill({ response: res, body: text });
  });
}

const ROW = "[data-account-preference='spoken_language']";
const SEL = `${ROW} select[data-account-preference-select='spoken_language']`;
const NOTE = `${ROW} [data-account-preference-saved='spoken_language']`;

async function openModal(page) {
  await page.locator("#users-table tr[data-user='alice'] button[data-action='preferences']").click();
  await page.waitForSelector(`#account-preferences-backdrop:not([hidden]) ${SEL}`, { timeout: 15000 });
}

const browser = await chromium.launch({ headless: true });
try {
  // ---- A. Preferences modal (alice) ----
  const { ctx, page } = await signIn(browser, "alice", ALICE, "light");
  await toUsers(page);
  const puts = recordPuts(page);
  const before = await prefs(page);
  const block = before.spoken_language;
  check(block && block.value === "auto" && block.label === "Spoken language" && Array.isArray(block.choices), "the gateway serves the spoken_language block (auto at first)", block);
  const served = (block.choices || []).map((c) => c.label);
  check(served[0] === "Auto (detected)" && (block.choices || []).some((c) => c.value === "fr" && c.label === "French"), "served choices: Auto (detected) first, fr = French", served);
  await openModal(page);
  const rows = await page.$$eval("#account-preferences-body [data-account-preference]", (els) => els.map((e) => e.getAttribute("data-account-preference")));
  check(rows[rows.length - 1] === "spoken_language" && rows[rows.length - 2] === "time_zone", "the Spoken language row comes right after the time-zone row", rows);
  check((await page.textContent(`${ROW} label`)).trim() === block.label, "label = the gateway's");
  check((await page.getAttribute(`${ROW} label`, "for")) === (await page.getAttribute(SEL, "id")), "the label names the select");
  check((await page.textContent(`${ROW} .help-q__text`)).trim() === block.help, "help = the gateway's sentence");
  check((await page.getAttribute(`${ROW} .help-q summary`, "aria-label")) === `What is “${block.label}”?`, "the ? is labelled like the app rows");
  const opts = await page.$$eval(`${SEL} option`, (els) => els.map((e) => [e.value, e.textContent]));
  check(JSON.stringify(opts) === JSON.stringify(block.choices.map((c) => [c.value, c.label])), "the options are exactly the served choices, labels verbatim", opts);
  check((await page.inputValue(SEL)) === "auto", "Auto (detected) selected while nothing is stored");
  check(await page.locator("#account-preferences-body button", { hasText: /^\s*save\s*$/i }).count() === 0, "no Save button");
  // Pick French: ONE PUT, "Saved.", stored.
  await page.selectOption(SEL, "fr");
  await page.waitForFunction((s) => { const n = document.querySelector(s); return n && n.textContent.trim() === "Saved."; }, NOTE, { timeout: 15000 });
  check(puts.length === 1 && puts[0] === JSON.stringify({ spoken_language: "fr" }), "a change is ONE PUT {spoken_language: fr}", puts);
  check((await page.getAttribute(NOTE, "class")).includes("ok"), "Saved. is the ok state");
  const afterFr = await prefs(page);
  check(afterFr.preferences.spoken_language === "fr" && afterFr.spoken_language.value === "fr", "French stored on the gateway", afterFr.preferences);
  check((await page.inputValue(SEL)) === "fr", "the re-rendered row shows French");
  // A reload: the stored value comes back from the gateway.
  await reload(page, "alice", ALICE);
  await toUsers(page);
  await openModal(page);
  check((await page.inputValue(SEL)) === "fr", "after a reload the stored value (French) is selected");
  // Back to Auto.
  puts.length = 0;
  await page.selectOption(SEL, "auto");
  await page.waitForFunction((s) => { const n = document.querySelector(s); return n && n.textContent.trim() === "Saved."; }, NOTE, { timeout: 15000 });
  check(puts.length === 1 && puts[0] === JSON.stringify({ spoken_language: "auto" }), "back to Auto = ONE PUT {spoken_language: auto}", puts);
  const backAuto = await prefs(page);
  check(backAuto.spoken_language.value === "auto", "Auto stored (the block says auto)", backAuto.spoken_language.value);
  // A refusal in the row: "Not saved. <the gateway's sentence>", the value restored.
  await forceUnknownCode(page);
  await page.selectOption(SEL, "de");
  await page.waitForFunction((s) => { const n = document.querySelector(s); return n && n.textContent.startsWith("Not saved. "); }, NOTE, { timeout: 15000 });
  const sentence = refusals.length ? refusals[refusals.length - 1].message : null;
  check(refusals.length === 1 && refusals[0].status === 400 && typeof sentence === "string" && sentence.startsWith("spoken_language = 'xx' refused: not a language the speech engines support. Choose auto or one of: "), "the gateway refuses an unknown code with its sentence", refusals);
  check((await page.textContent(NOTE)) === `Not saved. ${sentence}`, "Not saved. <the gateway's sentence>", await page.textContent(NOTE));
  check((await page.inputValue(SEL)) === "auto", "a refusal restores the shown value");
  await page.unroute("**/api/gateway/accounts/me/preferences");
  check((await prefs(page)).spoken_language.value === "auto", "a refusal stores nothing");
  await page.click("#account-preferences-close");
  // A gateway without the spoken_language block: the modal fails loud.
  await page.route("**/api/gateway/accounts/me/preferences", async (route) => {
    const res = await route.fetch();
    const body = await res.json();
    delete body.spoken_language;
    await route.fulfill({ response: res, body: JSON.stringify(body) });
  });
  await page.locator("#users-table tr[data-user='alice'] button[data-action='preferences']").click();
  await page.waitForSelector("#account-preferences-body .ui-alert.tone-err", { timeout: 15000 });
  const loud = (await page.textContent("#account-preferences-body .ui-alert")).trim();
  check(loud.includes("GET /accounts/{id}/preferences answered without a spoken_language block (R18 preferences seam)."), "a missing spoken_language block fails loud", loud);
  await page.unroute("**/api/gateway/accounts/me/preferences");
  await page.click("#account-preferences-close");
  await ctx.close();

  // ---- B. Multimodal page (admin, his own account) ----
  const m = await signIn(browser, "admin", ADMIN, "light");
  const mputs = recordPuts(m.page);
  await toMultimodal(m.page);
  const order = await m.page.evaluate(() => {
    const sec = document.getElementById("defaults-section");
    const kids = [...sec.children].map((e) => e.id || e.className);
    return { table: kids.findIndex((k) => String(k).includes("capability-table")), line: kids.indexOf("defaults-spoken-language-line"), msg: kids.indexOf("defaults-message") };
  });
  check(order.table >= 0 && order.line === order.table + 1 && order.msg === order.line + 1, "the line sits under the capability table, before the page message", order);
  check((await m.page.textContent("#defaults-spoken-language-line > label")).trim() === "Spoken language", "Multimodal label = Spoken language");
  check((await m.page.getAttribute("#defaults-spoken-language-line > label", "for")) === "defaults-spoken-language", "label for=defaults-spoken-language");
  check((await m.page.textContent("#defaults-spoken-language-line .help-q__text")).trim() === block.help, "Multimodal help = the gateway's sentence");
  check((await m.page.getAttribute("#defaults-spoken-language-note", "role")) === "status", "the inline note is a status");
  const mopts = await m.page.$$eval("#defaults-spoken-language option", (els) => els.map((e) => [e.value, e.textContent]));
  check(JSON.stringify(mopts) === JSON.stringify(block.choices.map((c) => [c.value, c.label])), "Multimodal options = the served choices", mopts.length);
  check((await m.page.inputValue("#defaults-spoken-language")) === "auto", "the admin's own value (auto) is selected");
  await m.page.selectOption("#defaults-spoken-language", "fr");
  await m.page.waitForFunction(() => { const n = document.getElementById("defaults-spoken-language-note"); return n && n.textContent.trim() === "Saved."; }, null, { timeout: 15000 });
  check(mputs.length === 1 && mputs[0] === JSON.stringify({ spoken_language: "fr" }), "Multimodal change = ONE PUT {spoken_language: fr}", mputs);
  const adminPrefs = await prefs(m.page);
  check(adminPrefs.spoken_language.value === "fr", "the admin's preference is stored", adminPrefs.spoken_language.value);
  await reload(m.page, "admin", ADMIN);
  await toMultimodal(m.page);
  check((await m.page.inputValue("#defaults-spoken-language")) === "fr", "after a reload the Multimodal line shows the stored value");
  await forceUnknownCode(m.page);
  await m.page.selectOption("#defaults-spoken-language", "en");
  await m.page.waitForFunction(() => { const n = document.getElementById("defaults-spoken-language-note"); return n && n.textContent.startsWith("Not saved. "); }, null, { timeout: 15000 });
  check((await m.page.textContent("#defaults-spoken-language-note")) === `Not saved. ${sentence}`, "Multimodal refusal = Not saved. <the gateway's sentence>");
  check((await m.page.inputValue("#defaults-spoken-language")) === "fr", "Multimodal refusal restores the shown value");
  await m.page.unroute("**/api/gateway/accounts/me/preferences");
  await m.ctx.close();
  {
    const a2 = await signIn(browser, "alice", ALICE, "light");
    check((await prefs(a2.page)).spoken_language.value === "auto", "alice's preference is untouched by the admin's");
    await a2.ctx.close();
  }

  // ---- Layout: both surfaces, 3 widths x 2 themes ----
  for (const theme of ["light", "dark"]) {
    for (const [w, h] of [[1440, 900], [834, 1112], [390, 844]]) {
      const o = await signIn(browser, "alice", ALICE, theme, w, h);
      await toUsers(o.page);
      await openModal(o.page);
      await o.page.locator(SEL).scrollIntoViewIfNeeded();
      if (SHOTS) await o.page.screenshot({ path: path.join(SHOTS, `web-preferences-spoken-${theme}-${w}.png`) });
      check(!(await o.page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1)), `Preferences: no horizontal page scroll at ${w} (${theme})`);
      await o.ctx.close();
      const a = await signIn(browser, "admin", ADMIN, theme, w, h);
      await toMultimodal(a.page);
      await a.page.locator("#defaults-spoken-language").scrollIntoViewIfNeeded();
      await a.page.waitForTimeout(300);
      if (SHOTS) await a.page.screenshot({ path: path.join(SHOTS, `web-multimodal-spoken-${theme}-${w}.png`) });
      check(!(await a.page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1)), `Multimodal: no horizontal page scroll at ${w} (${theme})`);
      await a.ctx.close();
    }
  }
} catch (e) {
  failures.push(`exception: ${e && e.stack ? e.stack : e}`);
} finally {
  await browser.close();
}
console.log(JSON.stringify({ failures, checks }));
process.exit(failures.length ? 1 : 0);
