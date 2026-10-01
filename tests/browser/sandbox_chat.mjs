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

const [BASE, ADMIN, PW] = process.argv.slice(2);
const require = createRequire(path.join(PW, "/"));
const { chromium } = require("playwright-core");

const failures = [];
let checks = 0;
function check(ok, what, detail) {
  checks += 1;
  if (!ok) failures.push(detail === undefined ? what : `${what}: ${JSON.stringify(detail)}`);
}

const browser = await chromium.launch();
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

  await page.goto(`${BASE}/console`);
  await page.waitForSelector("#login-form");
  await page.fill("#login-user", "admin");
  await page.fill("#login-token", ADMIN);
  await page.click("#login-button");
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
  console.log(JSON.stringify({ failures, checks, sandboxRequests }));
} finally {
  await browser.close();
}
