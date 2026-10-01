// Browser checks of the entity Manage modal (round 3 DESIGN-v3 §12), run by
// tests/test_gateway_console_browser_entity_manage.py against a hermetic scratch gateway that
// holds one entity (castor).
//
//   node entity_manage.mjs <base-url> <admin-token> <playwright-node-modules> <kit ui-kit dir>
//
// Prints one JSON line: {"failures": [...], "checks": N}.
import { createRequire } from "node:module";
import module from "node:module";
import fs from "node:fs";
import path from "node:path";

const [BASE, ADMIN, PW, KIT] = process.argv.slice(2);
if (!PW) throw new Error("entity_manage.mjs needs <base> <admin-token> <playwright node_modules> [kit dir]");
const require = createRequire(path.join(PW, "/"));
const pw = require("playwright-core");
const ENTITY = "castor";

let labelScaleSource = `
  window.checkLabelScale = function (root) {
    const sel = "label, .af-switch__label, .af-form__label, .af-field-caption, [data-af-caption]";
    const out = [];
    for (const el of root.querySelectorAll(sel)) {
      if (el.getClientRects().length === 0) continue;
      const cs = getComputedStyle(el);
      const size = parseFloat(cs.fontSize) || 0;
      const w = cs.fontWeight === "bold" ? 700 : (parseFloat(cs.fontWeight) || 400);
      if (size > 15.01 || w > 600) out.push({ text: (el.textContent || "").trim().slice(0, 80), fontSize: size, fontWeight: w });
    }
    return out;
  };`;
if (KIT && fs.existsSync(path.join(KIT, "src", "label_scale.ts"))) {
  const js = module.stripTypeScriptTypes(fs.readFileSync(path.join(KIT, "src", "label_scale.ts"), "utf8")).replace(/^export\s+/gm, "");
  labelScaleSource = `(function(){ ${js}\n window.checkLabelScale = (root) => checkLabelScale(root).map((h) => ({ text: h.text, fontSize: h.fontSize, fontWeight: h.fontWeight })); })();`;
}

const failures = [];
let checks = 0;
let step = "start";
function check(ok, what, detail) {
  checks += 1;
  if (!ok) failures.push(detail === undefined ? what : `${what}: ${JSON.stringify(detail)}`);
}

async function newPage(browser, viewport, touch = false) {
  const ctx = await browser.newContext(touch ? { viewport, hasTouch: true, isMobile: true } : { viewport });
  await ctx.addInitScript(labelScaleSource);
  const page = await ctx.newPage();
  page.on("pageerror", (e) => failures.push(`pageerror: ${e.message}`));
  return { ctx, page };
}

async function signIn(page) {
  await page.goto(`${BASE}/console`);
  await page.waitForSelector("#login-form");
  await page.fill("#login-user", "admin");
  await page.fill("#login-token", ADMIN);
  await page.click("#login-button");
  await page.waitForFunction(() => document.body.classList.contains("signed-in"), null, { timeout: 30000 });
  await page.keyboard.press("Escape").catch(() => {});
  await page.evaluate(() => document.getElementById("tab-button-users").click());
  try {
    await page.waitForSelector(`#users-section tr[data-user="${ENTITY}"]`, { timeout: 20000 });
  } catch (e) {
    const why = await page.evaluate(() => ({ users: (document.getElementById("users-message") || {}).textContent, rows: Array.from(document.querySelectorAll("#users-section tr[data-user]")).map((t) => t.getAttribute("data-user")), active: (document.querySelector(".tab-panel.active, section.active") || {}).id }));
    throw new Error(`the Accounts table has no visible row ${ENTITY}: ${JSON.stringify(why)}`);
  }
}

// The row's Manage action: a visible button on entity rows (accounts-web §1.1, C3F ruling).
async function openFromRow(page) {
  step = "open Manage from the row";
  await page.locator(`#users-section tr[data-user="${ENTITY}"] [data-action="manage"]`).click();
  await page.waitForSelector("#entity-manage-backdrop:not([hidden])", { timeout: 10000 });
  await page.waitForFunction(() => /Now:/.test(document.getElementById("entity-state-current").textContent || ""), null, { timeout: 20000 });
}

const VERB = /^(wake|sleep|pause|resume|start|stop|restore|turn on|turn off|enable|disable|on|off)\b/i;
const ROW_ACTIONS = /^(active|email|logs|archive|unarchive|delete|rotate)$/i;

async function visibleButtons(page) {
  return page.evaluate(() => Array.from(document.querySelectorAll("#entity-manage-section button"))
    .filter((b) => b.getClientRects().length > 0 && !b.closest("[hidden], .hidden"))
    .map((b) => ({ id: b.id, role: b.getAttribute("role") || "", text: (b.querySelector(".af-switch__label") || b).textContent.trim() })));
}

const browser = await pw.chromium.launch({ headless: true });
try {
  // ---- Desktop 1440: opens as a modal over Accounts, never navigates away.
  {
    const { ctx, page } = await newPage(browser, { width: 1440, height: 900 });
    await signIn(page);
    const urlBefore = page.url();
    await openFromRow(page);
    const shape = await page.evaluate(() => {
      const bd = document.getElementById("entity-manage-backdrop");
      const dlg = bd.querySelector('[role="dialog"]');
      const users = document.getElementById("users-section");
      const r = dlg.getBoundingClientRect();
      return {
        modal: dlg.getAttribute("aria-modal"), classes: dlg.className, title: document.getElementById("entity-manage-title").textContent,
        backdropFixed: getComputedStyle(bd).position, usersShown: getComputedStyle(users).display !== "none" && !users.classList.contains("hidden"),
        rowRendered: Boolean(users.querySelector("tr[data-user]")) && users.querySelector("tr[data-user]").getClientRects().length > 0,
        width: r.width, focusInside: dlg.contains(document.activeElement), focusId: document.activeElement && document.activeElement.id,
        rawMoments: /\b[a-z]+_[a-z_]*changed\b/.test(document.getElementById("entity-overview").textContent || ""), filter: getComputedStyle(bd).backdropFilter || getComputedStyle(bd).webkitBackdropFilter || "",
      };
    });
    check(shape.modal === "true", "Manage is role=dialog aria-modal=true", shape);
    check(/\baf-modal\b/.test(shape.classes) && /\baf-modal--wide\b/.test(shape.classes), "Manage uses the kit af-modal af-modal--wide", shape.classes);
    check(shape.title === `Manage — ${ENTITY}`, "title says Manage — <name>", shape.title);
    check(shape.backdropFixed === "fixed", "the backdrop covers the page", shape.backdropFixed);
    check(/blur/.test(shape.filter), "the backdrop is blurred", shape.filter);
    check(shape.usersShown && shape.rowRendered, "the Accounts table stays visible behind the modal", shape);
    check(shape.width >= 1000, "the modal is wide at 1440", shape.width);
    check(shape.focusInside && shape.focusId === "entity-manage-title", "focus moves into the modal, on its title", shape.focusId);
    check(!shape.rawMoments, "recent moments are plain words, not event codes");
    check(page.url() === urlBefore, "opening Manage does not navigate", page.url());
    const hits = await page.evaluate(() => window.checkLabelScale(document.getElementById("entity-manage-section")));
    check(hits.length === 0, "labels in the modal keep the type scale (<= 15 px, <= 600)", hits);

    // ---- Every tab: no verb toggles, no Save buttons, nothing that duplicates the Accounts row.
    for (const tab of ["overview", "talk", "lifecycle", "substrate", "tools", "prompt"]) {
      step = `tab ${tab}`;
      await page.click(`#entity-subtab-${tab}`);
      await page.waitForTimeout(150);
      const selected = await page.getAttribute(`#entity-subtab-${tab}`, "aria-selected");
      check(selected === "true", `tab ${tab} is selected (aria-selected)`, selected);
      for (const b of await visibleButtons(page)) {
        if (b.role === "switch" || b.role === "tab" || b.id === "entity-manage-close") continue;
        check(!VERB.test(b.text), `tab ${tab}: no verb toggle button`, b);
        check(!/^save\b/i.test(b.text), `tab ${tab}: no Save button`, b);
        check(!ROW_ACTIONS.test(b.text), `tab ${tab}: nothing duplicating the Accounts row`, b);
      }
    }
    for (const id of ["entity-state-awake", "entity-owntime-toggle"]) {
      const role = await page.getAttribute(`#${id}`, "role");
      check(role === "switch", `${id} is a switch`, role);
    }

    // ---- Awake switch: on wakes, off asks inline first, then sleeps.
    step = "Awake switch";
    await page.click("#entity-subtab-lifecycle");
    const before = await page.getAttribute("#entity-state-awake", "aria-checked");
    if (before === "true") {
      await page.click("#entity-state-awake");
      check(await page.isVisible("#entity-sleep-confirm"), "switching Awake off asks first (inline)");
      await page.click("#entity-sleep-now");
    } else {
      await page.click("#entity-state-awake");
    }
    await page.waitForFunction((was) => document.getElementById("entity-state-awake").getAttribute("aria-checked") !== was, before, { timeout: 15000 }).catch(() => {});
    const after = await page.getAttribute("#entity-state-awake", "aria-checked");
    check(after !== before, "the Awake switch changes the entity's state", { before, after });
    if (after === "true") {
      await page.click("#entity-state-awake");
      check(await page.isVisible("#entity-sleep-confirm"), "switching Awake off asks first (inline)");
      check(await page.getAttribute("#entity-state-awake", "aria-checked") === "true", "the switch does not move before the confirmation");
      await page.click("#entity-sleep-cancel");
      check(!(await page.isVisible("#entity-sleep-confirm")), "Cancel closes the inline confirmation");
    }

    // ---- A failed save says why (the API message), auto-save says Saved.
    step = "substrate save";
    await page.click("#entity-subtab-substrate");
    await page.route("**/api/gateway/entities/*/substrate", (route) => (route.request().method() === "PUT"
      ? route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: { reason_code: "test", message: "Refused by the browser test." } }) })
      : route.continue()));
    // The Mind is the kit's shared picker: Custom, then provider and model.
    const mind = page.locator("#entity-mind-picker");
    check(await mind.locator('[role="tab"][aria-selected="true"]').textContent() === "Gateway default", "the Mind picker starts on Gateway default");
    await mind.locator('[role="tab"]', { hasText: "Custom" }).click();
    const pickCustom = async (label, value) => {
      await mind.locator(`button[aria-label="${label}"]`).click();
      await page.keyboard.type(value);
      await page.keyboard.press("Enter");
    };
    await pickCustom("Provider", "lmstudio");
    await pickCustom("Model", "some/model");
    await page.waitForFunction(() => /Refused by the browser test/.test(document.getElementById("entity-substrate-out").textContent || ""), null, { timeout: 10000 }).catch(() => {});
    const err = await page.textContent("#entity-substrate-out");
    check(/Not saved: Refused by the browser test\./.test(err || ""), "a failed save shows the API message", err);
    await page.unroute("**/api/gateway/entities/*/substrate");
    await pickCustom("Model", "other/model");
    await page.waitForFunction(() => /^Saved/.test(document.getElementById("entity-substrate-out").textContent || ""), null, { timeout: 10000 }).catch(() => {});
    check(/^Saved/.test((await page.textContent("#entity-substrate-out")) || ""), "a change saves itself and says Saved", await page.textContent("#entity-substrate-out"));

    // ---- Focus trap: Tab never leaves the dialog.
    let escaped = 0;
    const escapedTo = [];
    for (let i = 0; i < 40; i++) {
      await page.keyboard.press("Tab");
      const where = await page.evaluate(() => {
        const a = document.activeElement;
        return document.getElementById("entity-manage-section").contains(a) ? "" : `${a ? a.tagName : "?"}#${a && a.id}.${a && a.className}`;
      });
      if (where) { escaped += 1; escapedTo.push(where); }
    }
    check(escaped === 0, "focus stays trapped inside the modal", escapedTo);

    // ---- Esc closes, focus returns to the row.
    step = "Esc";
    await page.keyboard.press("Escape");
    await page.waitForTimeout(200);
    const closed = await page.evaluate(() => ({
      hidden: document.getElementById("entity-manage-backdrop").hidden,
      inRow: Boolean(document.activeElement && document.activeElement.closest && document.activeElement.closest('#users-section tr[data-user="castor"]')),
      active: document.activeElement && (document.activeElement.getAttribute("aria-label") || document.activeElement.textContent || "").trim().slice(0, 40),
    }));
    check(closed.hidden, "Esc closes the modal");
    check(closed.inRow, "closing returns focus to the entity's row", closed);

    // ---- Backdrop click closes too.
    step = "backdrop";
    await openFromRow(page);
    await page.mouse.click(10, 450);
    await page.waitForTimeout(200);
    check(await page.evaluate(() => document.getElementById("entity-manage-backdrop").hidden), "a backdrop click closes the modal");
    await ctx.close();
  }
  // ---- Phone 390: a full-screen sheet, no horizontal scroll.
  {
    const { ctx, page } = await newPage(browser, { width: 390, height: 844 }, true);
    await signIn(page);
    await page.evaluate((id) => openEntityManage(id), ENTITY);
    await page.waitForSelector("#entity-manage-backdrop:not([hidden])");
    await page.waitForTimeout(600);
    for (const tab of ["overview", "lifecycle", "substrate", "tools", "prompt"]) {
      await page.evaluate((t) => document.getElementById(`entity-subtab-${t}`).click(), tab);
      await page.waitForTimeout(150);
      const m = await page.evaluate(() => {
        const dlg = document.getElementById("entity-manage-section");
        const r = dlg.getBoundingClientRect();
        return { w: r.width, h: r.height, x: r.left, overflow: document.documentElement.scrollWidth > window.innerWidth || dlg.scrollWidth > dlg.clientWidth + 1 };
      });
      check(m.w >= 389 && m.x <= 0.5 && m.h >= 843, `phone ${tab}: Manage is a full-screen sheet`, m);
      check(!m.overflow, `phone ${tab}: no horizontal scroll`, m);
    }
    await ctx.close();
  }
} catch (e) {
  failures.push(`aborted at ${step}: ${String((e && e.message) || e).split("\n")[0]}`);
} finally {
  await browser.close();
}
console.log(JSON.stringify({ failures, checks }));
