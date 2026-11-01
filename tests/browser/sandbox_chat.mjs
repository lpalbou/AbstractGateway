// Browser checks of the console Sandbox on the kit chat island (DESIGN-v3 §8,
// round 3), run by tests/test_gateway_console_browser_sandbox.py against a
// hermetic scratch gateway whose input.text route points at a fake
// OpenAI-compatible server (no model anywhere).
//
//   node sandbox_chat.mjs <base-url> <admin-token> <playwright-node-modules>
//
// Prints one JSON line: {"failures": [...], "checks": N, "sandboxRequests": N}.
import { createRequire } from "node:module";
import path from "node:path";
import { mkdirSync } from "node:fs";

const [BASE, ADMIN, PW] = process.argv.slice(2);
const require = createRequire(path.join(PW, "/"));
const { chromium } = require("playwright-core");

const failures = [];
let checks = 0;
function check(ok, what, detail) {
  checks += 1;
  if (!ok) failures.push(detail === undefined ? what : `${what}: ${JSON.stringify(detail)}`);
}

const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined });
try {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  page.on("pageerror", (e) => failures.push(`pageerror: ${e.message}`));
  // Never probe the machine's local engines: the only provider is the fake endpoint.
  await page.route(/\/api\/gateway\/discovery\/providers(\?.*)?$/, (r) => r.fulfill({ json: { items: [{ name: "endpoint:fake", display_name: "Fake", available: true }] } }));
  await page.route(/\/api\/gateway\/engines(\/jobs)?(\?.*)?$/, (r) => r.fulfill({ status: 503, json: { detail: "browser test: engines are not probed" } }));
  await page.route(/\/api\/gateway\/host\/state(\?.*)?$/, (r) => r.fulfill({ status: 503, json: { detail: "browser test: host is not probed" } }));
  let sandboxRequests = 0;
  page.on("request", (req) => { if (req.method() === "POST" && req.url().endsWith("/api/gateway/sandbox/generate")) sandboxRequests += 1; });

  // This suite verifies chat widgets, independently of the first-run sign-in UI.
  const login = await page.request.post(`${BASE}/api/gateway/session/login`, {
    headers: { Origin: BASE }, data: { user_id: "admin", token: ADMIN },
  });
  if (!login.ok()) throw new Error(`fixture sign-in failed: HTTP ${login.status()}`);
  await page.goto(`${BASE}/console`);
  await page.waitForFunction(() => document.body.classList.contains("signed-in"), null, { timeout: 30000 });
  await page.evaluate(() => document.getElementById("tab-button-sandbox").click());
  await page.waitForTimeout(800);

  // 1. The island is mounted (the kit's panel-chat thread + composer).
  const root = page.locator("#sandbox-chat-root");
  check(await root.locator(".af-sandbox-chat.pc-workflow-chat").count() === 1, "the kit Sandbox chat island is mounted in #sandbox-chat-root");
  check(await root.locator(".pc-chat-thread").count() === 1, "panel-chat thread present");
  const textarea = root.locator("textarea.pc-composer__textarea");
  check(await textarea.count() === 1, "panel-chat composer textarea present");
  check(await root.getByRole("button", { name: "Attach file" }).count() === 1, "standard Attach control present");
  check(await page.locator("#sandbox-prompt, #sandbox-run, #sandbox-transcript").count() === 0, "the console's own composer is gone");
  check(await root.locator(".pc-composer--compact").count() === 1, "Sandbox uses the rounded shared composer by default");
  check(await textarea.getAttribute("rows") === "2", "the default composer starts with multiple text lines");
  check(await root.locator(".pc-composer__send--compact svg").count() === 1, "the shared send button has its accessible icon");
  const draft = "Compare these options\nKeep the accessible choice\nExplain the difference";
  await textarea.fill(draft);
  for (const viewport of [{ width: 1280, height: 800 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport);
    await textarea.scrollIntoViewIfNeeded();
    const composer = root.locator(".pc-composer");
    const geometry = await composer.evaluate((element) => {
      const box = element.getBoundingClientRect();
      const field = element.querySelector("textarea");
      const send = element.querySelector(".pc-composer__send--compact");
      return { width: box.width, left: box.left, right: box.right, viewport: innerWidth,
        radius: parseFloat(getComputedStyle(element).borderRadius), lines: field.value.split("\n").length,
        sendRadius: send ? parseFloat(getComputedStyle(send).borderRadius) : null, sendWidth: send ? send.getBoundingClientRect().width : 0 };
    });
    check(geometry.left >= -1 && geometry.right <= geometry.viewport + 1, `composer fits at ${viewport.width}px`, geometry);
    check(geometry.radius >= 24 && geometry.sendRadius >= geometry.sendWidth / 2 - 1, `rounded input and circular send survive console styles at ${viewport.width}px`, geometry);
    check((await textarea.inputValue()) === draft && geometry.lines === 3, `multiline draft survives resize at ${viewport.width}px`);
    if (process.env.ABSTRACTGATEWAY_COMPOSER_SHOTS) {
      mkdirSync(process.env.ABSTRACTGATEWAY_COMPOSER_SHOTS, { recursive: true });
      await composer.screenshot({ path: path.join(process.env.ABSTRACTGATEWAY_COMPOSER_SHOTS, `console-composer-${viewport.width}.png`) });
    }
  }
  await page.setViewportSize({ width: 1440, height: 900 });

  // 2. Every output mode the console offers is still a button around the island.
  const modes = await page.evaluate(() => ({
    expected: sandboxCandidateRows().map((row) => sandboxRouteShortLabel(row)),
    buttons: [...document.querySelectorAll("#sandbox-output-modes .sandbox-mode")].map((b) => ({ label: b.querySelector(".sandbox-mode-main")?.textContent || "", disabled: b.disabled, unconfigured: b.classList.contains("is-unconfigured") })),
  }));
  check(modes.expected.length >= 2 && modes.expected[0] === "Text", "the sandbox offers Text plus the configured media modes", modes.expected);
  check(JSON.stringify(modes.buttons.map((b) => b.label)) === JSON.stringify(modes.expected), "one mode button per output mode, in order", modes);
  check(modes.buttons.every((b) => !b.disabled), "no mode button is greyed out", modes.buttons);
  for (const id of ["sandbox-system", "sandbox-reasoning", "sandbox-speculation"]) check(await page.locator(`#${id}`).isVisible(), `text mode keeps #${id}`);

  // 3. A text send goes through POST /sandbox/generate and the reply renders in the thread.
  if (await textarea.count() !== 1) {
    console.log(JSON.stringify({ failures, checks, sandboxRequests }));
    process.exit(0);
  }
  await textarea.fill("Say hello from the browser test");
  await textarea.press("Enter");
  await page.waitForFunction(() => /Fake reply/.test(document.getElementById("sandbox-chat-root").textContent || ""), null, { timeout: 60000 }).catch(() => {});
  check(sandboxRequests === 1, "the text send posted /api/gateway/sandbox/generate once", sandboxRequests);
  const thread = await root.locator(".pc-chat-thread").innerText();
  check(/Say hello from the browser test/.test(thread), "the user's message is in the thread", thread.slice(0, 400));
  check(/Fake reply/.test(thread) && /Say hello from the browser test/.test(thread), "the fake model's reply is in the thread", thread.slice(0, 400));
  check(await root.locator(".pc-chat-item--assistant .pc-chat-body strong").count() >= 1, "the reply's markdown is rendered by the kit card");
  check((await textarea.inputValue()) === "", "the draft is cleared after the send");

  // 4. An unconfigured mode stays choosable and says why it cannot send.
  const unconfigured = modes.buttons.findIndex((b) => b.unconfigured);
  check(unconfigured >= 0, "the fixture has at least one unconfigured mode (so the blocked notice is exercised)", modes.buttons);
  if (unconfigured >= 0) {
    await page.locator("#sandbox-output-modes .sandbox-mode").nth(unconfigured).click();
    const notice = await root.locator(".pc-workflow-chat__blocked").innerText().catch(() => "");
    check(/is not configured yet/.test(notice), "an unconfigured mode shows the blocked notice", notice);
    check(await textarea.isDisabled(), "the composer is disabled while the mode cannot send");
    check(!(await page.locator("#sandbox-system").isVisible()), "media modes hide the text-only settings");
    await page.locator("#sandbox-output-modes .sandbox-mode").first().click();
  }

  // 5. Clear empties the thread.
  await root.getByRole("button", { name: "Clear chat" }).click();
  check(await root.locator(".pc-chat-item").count() === 0, "Clear empties the thread");
  // The console's Docs drawer is a second island over the same default composer.
  await page.getByRole("button", { name: "Docs assistant", exact: true }).click();
  const docs = page.locator(".pc-docs-assistant");
  await docs.waitFor({ state: "visible" });
  check(await docs.locator(".pc-composer--compact").count() === 1, "Docs assistant uses the rounded shared default too");
  check(await docs.locator("textarea").getAttribute("rows") === "2", "Docs assistant does not pin the old single-line layout");
  check(await docs.locator(".pc-composer__send--compact svg").count() === 1, "Docs assistant retains the shared accessible send icon");
  console.log(JSON.stringify({ failures, checks, sandboxRequests }));
} finally {
  await browser.close();
}
