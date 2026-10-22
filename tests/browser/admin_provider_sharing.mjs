import { createRequire } from "node:module";
import path from "node:path";

const [BASE, ADMIN, PW, BOB] = process.argv.slice(2);
const require = createRequire(path.join(PW, "/"));
const { chromium } = require("playwright-core");
const browser = await chromium.launch();
let checks = 0;
function check(value, message) {
  if (!value) throw new Error(message);
  checks++;
}
try {
  for (const [user, token, scope] of [["admin", ADMIN, "gateway"], ["bob", BOB, "user"]]) {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    // Keep the check about provider access: engine status and unrelated model
    // pickers never probe or control real installed providers.
    await page.route("**/api/gateway/engines**", (route) => route.fulfill({ json: { engines: [], jobs: [] } }));
    await page.route("**/api/gateway/discovery/providers**", (route) => route.fulfill({ json: { items: [] } }));
    await page.goto(`${BASE}/console#providers`);
    await page.locator("#login-user").fill(user);
    await page.locator("#login-token").fill(token);
    await page.locator("#login-button").click();
    await page.waitForFunction(() => document.body.classList.contains("signed-in"));
    await page.waitForFunction(() => document.getElementById("endpoint-profiles-table")?.textContent.includes("endpoint:lmstudio-lan"));
    const providers = await page.locator("#endpoint-profiles-table").innerText();
    check(providers.includes("endpoint:openai"), `${user} sees shared OpenAI provider`);
    check(providers.includes("endpoint:lmstudio-lan"), `${user} sees shared LM Studio provider`);
    await page.locator("#provider-preset-openai-compatible").click();
    await page.locator("#provider-modal-backdrop:not(.hidden)").waitFor();
    check(await page.locator("#endpoint-scope").inputValue() === scope, `${user} gets the correct connection scope`);
    const options = await page.locator("#endpoint-scope option").evaluateAll((els) => els.map((el) => ({ value: el.value, text: el.textContent })));
    check(options.length === 1 && options[0].value === scope, `${user} sees only the applicable sharing option`);
    if (user === "admin") check(options[0].text === "Everyone on this gateway", "admin sees explicit shared-access wording");
    check(errors.length === 0, `${user}: no browser errors: ${errors.join(", ")}`);
    await context.close();
  }
} finally {
  await browser.close();
}
console.log(JSON.stringify({ checks }));
