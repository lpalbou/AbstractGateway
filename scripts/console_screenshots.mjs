// Headless renders of the gateway console + peer apps for design-charter
// comparison (charter refactor 2026-07-15). RUN FROM abstractobserver/ so
// playwright resolves: cd ../abstractobserver && node ../abstractgateway/scripts/console_screenshots.mjs <outdir> [prefix]
import { chromium } from "playwright";
import fs from "node:fs";

const OUT = process.argv[2] || "/tmp/console_shots";
const PREFIX = process.argv[3] || "";
const TOKEN = JSON.parse(fs.readFileSync("/Users/albou/tmp/abstractframework/runtime/dev/gateway-user-tokens.json", "utf8")).users["default:admin"].token;

const VIEW = { width: 1560, height: 980 };

async function shot(page, name) {
  await page.waitForTimeout(450);
  await page.screenshot({ path: `${OUT}/${PREFIX}${name}.png`, fullPage: false });
  console.log("shot", name);
}

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: VIEW });

// ---- Console ----
await page.goto("http://127.0.0.1:8080/console", { waitUntil: "networkidle" });
await shot(page, "console-login");

// Sign in through the real form (the operator's path).
await page.fill("#login-token", TOKEN);
await page.click("#login-button");
await page.waitForTimeout(1800);
await shot(page, "console-users");

// Create-user modal
try {
  await page.click("#open-create-user", { timeout: 3000 });
  await shot(page, "console-modal-create-user");
  await page.click("#create-user-cancel");
} catch (e) { console.log("skip create-user modal:", e.message); }

// Summon-entity modal
try {
  await page.click("#open-create-entity", { timeout: 3000 });
  await shot(page, "console-modal-create-entity");
  await page.click("#entity-create-cancel");
} catch (e) { console.log("skip entity modal:", e.message); }

// Entity manage drill-in (first entity's Manage button lives in the table;
// use Talk-adjacent Manage via JS click on the second button of row 1).
try {
  await page.evaluate(() => {
    const row = document.querySelector("#entities-table tr");
    if (row) { const btns = row.querySelectorAll("button"); if (btns[1]) btns[1].click(); }
  });
  await page.waitForTimeout(2500);
  await shot(page, "console-entity-manage");
  await page.click("#entity-subtab-lifecycle");
  await shot(page, "console-entity-lifecycle");
  await page.click("#entity-manage-close");
} catch (e) { console.log("skip manage:", e.message); }

for (const [tab, name] of [["runtimes", "console-runtimes"], ["providers", "console-providers"], ["defaults", "console-defaults"], ["sandbox", "console-sandbox"]]) {
  try {
    await page.click(`#tab-button-${tab}`);
    await page.waitForTimeout(1400);
    await shot(page, name);
  } catch (e) { console.log("skip", tab, e.message); }
}

// ---- Peer apps (charter references) ----
for (const [url, name] of [
  ["http://127.0.0.1:3000/", "peer-flow"],
  ["http://127.0.0.1:3001/", "peer-observer"],
  ["http://127.0.0.1:3003/", "peer-continuum"],
]) {
  try {
    await page.goto(url, { waitUntil: "networkidle", timeout: 15000 });
    await shot(page, name);
  } catch (e) { console.log("skip", name, e.message); }
}

await browser.close();
console.log("done");
