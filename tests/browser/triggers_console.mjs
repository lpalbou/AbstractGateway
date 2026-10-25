// Root backlog 1041: the console's Triggers page in a real browser against a hermetic scratch
// gateway. The trigger-connection routes (W2's contract) are served by an in-page fake through
// Playwright routing, so the page is proven against the exact shapes whether or not the gateway
// under test already serves them: list, create (webhook secret shown once), recent events,
// test-fire (visible result, the event appears in the expanded list), delete with confirmation,
// the admin's All accounts view, and `#triggers?new=<source_id>`.
//
//   node triggers_console.mjs <base-url> <admin-token> <playwright-node-modules> [screenshot dir]
//
// Prints one JSON line: {"failures": [...], "checks": N, "shots": [...]}.
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";

const [BASE, ADMIN, PW, SHOTS] = process.argv.slice(2);
if (!PW) throw new Error("triggers_console.mjs <base> <admin-token> <playwright-node-modules> [shots dir]");
const require = createRequire(path.join(PW, "/"));
const { chromium } = require("playwright-core");

const failures = [];
const shots = [];
let checks = 0;
function check(ok, what, detail) {
  checks += 1;
  if (!ok) failures.push(detail === undefined ? what : `${what}: ${JSON.stringify(detail)}`);
}

const SOURCES = {
  items: [
    { id: "schedule", version: 2, label: "Schedule", available: true, needs_connection: false, connection_schema: {}, config_schema: { type: "object" }, event_schema: {}, capabilities: { kind: "time" } },
    { id: "email.received", version: 1, label: "Email received", available: true, needs_connection: true, config_schema: { type: "object" }, event_schema: {}, capabilities: { kind: "event" },
      connection_schema: { type: "object", required: ["account"], properties: { account: { type: "string", title: "Mailbox", description: "The mailbox to read (self = your own address).", default: "self" } } } },
    { id: "feed.item", version: 1, label: "Feed item", available: true, needs_connection: true, config_schema: { type: "object" }, event_schema: {}, capabilities: { kind: "event" },
      connection_schema: { type: "object", required: ["url"], properties: {
        url: { type: "string", title: "Feed address", description: "An RSS or Atom address.", pattern: "^https?://", examples: ["https://example.org/feed.xml"] },
        every_minutes: { type: "integer", title: "Check every (minutes)", default: 15, minimum: 5 } } } },
    { id: "webhook.received", version: 1, label: "Webhook received", available: true, needs_connection: true, config_schema: { type: "object" }, event_schema: {}, capabilities: { kind: "event" },
      connection_schema: { type: "object", properties: { subject_path: { type: "string", title: "Subject from", description: "Optional JSON path in the body used as the event subject, e.g. repository.full_name." } } } },
  ],
};

// The fake store behind the contract routes (per browser context).
function fakeStore() {
  const t0 = Date.now();
  const iso = (msAgo) => new Date(t0 - msAgo).toISOString();
  const s = {
    seq: 3, n: 2,
    rows: [
      { connection_id: "conn_feed1", source_id: "feed.item", source_version: 1, label: "Release notes feed", status: "active", created_at: iso(86400000), last_event_at: iso(240000), config_public: { url: "https://example.org/releases.xml", every_minutes: 15 }, principal: "default:admin" },
      { connection_id: "conn_mail1", source_id: "email.received", source_version: 1, label: "My mailbox", status: "active", created_at: iso(86400000 * 3), last_event_at: null, config_public: { account: "self" }, principal: "default:admin" },
    ],
    others: [
      { connection_id: "conn_bob1", source_id: "webhook.received", source_version: 1, label: "Bob's CI hook", status: "paused", created_at: iso(3600000), last_event_at: iso(3600000), config_public: {}, ingress_url: `${BASE}/api/gateway/triggers/webhooks/conn_bob1`, principal: "default:bob" },
    ],
    events: {
      conn_feed1: [1, 2, 3].map((i) => ({ specversion: "1.0", id: `evt_feed_${i}`, type: "abstractframework.feed.item", source: "trigger:conn_feed1", subject: `Release 0.1${i}`, time: iso(240000 + (3 - i) * 3600000), datacontenttype: "application/json",
        data: { title: `Release 0.1${i}`, link: `https://example.org/releases/0.1${i}` }, connectionid: "conn_feed1", sequence: i, dedupekey: `guid-${i}`, abstracttrust: "external" })),
    },
    requests: [],
  };
  return s;
}

async function mockRoutes(ctx, store) {
  await ctx.route("**/api/gateway/trigger-sources", (route) => route.fulfill({ json: SOURCES }));
  await ctx.route("**/api/gateway/triggers/connections**", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const method = req.method();
    store.requests.push(`${method} ${url.pathname}${url.search}`);
    const parts = url.pathname.split("/").filter(Boolean); // api gateway triggers connections [id] [events|test-fire]
    const id = parts[4];
    const sub = parts[5];
    if (!id && method === "GET") {
      const all = url.searchParams.get("all") === "true";
      return route.fulfill({ json: { connections: all ? store.rows.concat(store.others) : store.rows } });
    }
    if (!id && method === "POST") {
      const body = JSON.parse(req.postData() || "{}");
      store.n += 1;
      const cid = `conn_new${store.n}`;
      const row = { connection_id: cid, source_id: body.source_id, source_version: body.source_version, label: body.label, status: "active", created_at: new Date().toISOString(), last_event_at: null, config_public: body.config, principal: "default:admin" };
      if (body.source_id === "webhook.received") row.ingress_url = `${BASE}/api/gateway/triggers/webhooks/${cid}`;
      store.rows.push(row);
      store.lastCreate = body;
      return route.fulfill({ json: Object.assign({}, row, body.source_id === "webhook.received" ? { secret: "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw" } : {}) });
    }
    const row = store.rows.concat(store.others).find((r) => r.connection_id === id);
    if (!row) return route.fulfill({ status: 404, json: { detail: { message: "No such connection." } } });
    if (method === "DELETE" && !sub) {
      store.rows = store.rows.filter((r) => r.connection_id !== id);
      return route.fulfill({ json: { deleted: true } });
    }
    if (sub === "events" && method === "GET") return route.fulfill({ json: { events: store.events[id] || [] } });
    if (sub === "test-fire" && method === "POST") {
      store.seq += 1;
      const ev = { specversion: "1.0", id: `evt_test_${store.seq}`, type: "abstractframework.trigger.test", source: `trigger:${id}`, subject: "Test event", time: new Date().toISOString(), datacontenttype: "application/json", data: { test: true }, connectionid: id, sequence: store.seq, abstracttrust: "user" };
      (store.events[id] = store.events[id] || []).push(ev);
      row.last_event_at = ev.time;
      return route.fulfill({ json: { event_id: ev.id, sequence: ev.sequence } });
    }
    return route.fulfill({ status: 405, json: { detail: "unexpected" } });
  });
}

async function newPage(browser, viewport, theme = "light") {
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: 1 });
  await ctx.addInitScript((t) => { try { localStorage.setItem("abstractgateway_ui_settings_v1", JSON.stringify({ theme: t })); } catch {} }, theme);
  await ctx.grantPermissions(["clipboard-read", "clipboard-write"], { origin: BASE }).catch(() => {});
  const store = fakeStore();
  await mockRoutes(ctx, store);
  const page = await ctx.newPage();
  page.on("pageerror", (e) => failures.push(`pageerror: ${e.message}`));
  return { ctx, page, store };
}
async function signIn(page, user, token, hash = "") {
  await page.goto(`${BASE}/console${hash}`);
  await page.waitForSelector("#login-form");
  await page.fill("#login-user", user);
  await page.fill("#login-token", token);
  await page.click("#login-button");
  await page.waitForFunction(() => document.body.classList.contains("signed-in"), null, { timeout: 30000 });
  // (No Escape after a deep link: it would close the dialog the link opened.)
  if (!hash) await page.keyboard.press("Escape").catch(() => {});
  await page.waitForTimeout(300);
}
async function openTab(page, tab) {
  await page.evaluate((t) => { document.body.classList.remove("nav-open"); document.getElementById(`tab-button-${t}`).click(); }, tab);
  await page.waitForSelector(`#tab-${tab}.active`);
}
const GROW = "html,body,.shell,.shell_main,.shell_content{height:auto!important;max-height:none!important;overflow:visible!important}.shell_topbar,.shell_header{position:static!important}";
async function shot(page, name) {
  if (!SHOTS) return;
  fs.mkdirSync(SHOTS, { recursive: true });
  const file = path.join(SHOTS, `${name}.png`);
  const tag = await page.addStyleTag({ content: GROW });
  await page.waitForTimeout(150);
  await page.screenshot({ path: file, fullPage: true });
  await tag.evaluate((el) => el.remove());
  shots.push(file);
}
async function shotEl(page, selector, name) {
  if (!SHOTS) return;
  fs.mkdirSync(SHOTS, { recursive: true });
  const file = path.join(SHOTS, `${name}.png`);
  await page.locator(selector).screenshot({ path: file });
  shots.push(file);
}
async function noSideScroll(page, what) {
  const over = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  check(over <= 1, `no horizontal scroll: ${what}`, over);
}

const browser = await chromium.launch();
try {
  for (const [w, h, theme] of [[1440, 900, "light"], [1440, 900, "dark"], [390, 844, "light"]]) {
    const tag = `${w}-${theme}`;
    const { ctx, page, store } = await newPage(browser, { width: w, height: h }, theme);
    await signIn(page, "admin", ADMIN);
    await openTab(page, "triggers");
    await page.waitForSelector('#triggers-table [data-trg-row="conn_feed1"]', { timeout: 30000 });
    check((await page.textContent("#page-title")).trim() === "Triggers", `${tag}: page title`);
    const feedRow = page.locator('[data-trg-row="conn_feed1"]');
    check((await feedRow.textContent()).includes("Feed item"), `${tag}: source label from the descriptor`);
    check((await feedRow.textContent()).includes("Release notes feed"), `${tag}: name`);
    check((await page.locator('[data-trg-row="conn_mail1"]').textContent()).includes("Never"), `${tag}: never-fired connection says Never`);
    await noSideScroll(page, `${tag} list`);
    // Events: expand, 3 events newest first, the JSON behind a disclosure.
    await feedRow.locator("[data-trg-events]").click();
    await page.waitForSelector('[data-trg-detail="conn_feed1"] [data-trg-event]');
    const seqs = await page.$$eval('[data-trg-detail="conn_feed1"] .trg-seq', (els) => els.map((e) => e.textContent));
    check(JSON.stringify(seqs) === JSON.stringify(["#3", "#2", "#1"]), `${tag}: events newest first`, seqs);
    await page.locator('[data-trg-event="evt_feed_3"] summary').click();
    check((await page.locator('[data-trg-event="evt_feed_3"] pre').textContent()).includes('"title": "Release 0.13"'), `${tag}: event JSON expands`);
    // Test: a visible result in the row, and the event appears in the expanded list.
    await feedRow.locator("[data-trg-test]").click();
    await page.waitForSelector('[data-trg-row="conn_feed1"] .trg-fire.is-ok');
    check((await page.locator('[data-trg-row="conn_feed1"] .trg-fire').textContent()).trim() === "Test event #4 sent.", `${tag}: test result shown`);
    await page.waitForSelector('[data-trg-event="evt_test_4"]');
    check((await page.locator('[data-trg-event="evt_test_4"] summary').textContent()).includes("test"), `${tag}: test event listed with its chip`);
    check((await page.locator('[data-trg-row="conn_feed1"] td.trg-last').textContent()).trim() === "just now" || w < 1024, `${tag}: last event refreshed`);
    if (w === 1440) await shot(page, `triggers-list-events-${tag}`);
    else await shot(page, `triggers-list-${tag}`);

    if (w === 1440 && theme === "light") {
      // Create a feed connection: the form comes from connection_schema.
      await page.click("#triggers-new");
      await page.waitForSelector("#trigger-modal-backdrop:not([hidden]) [data-trg-source]");
      const opts = await page.$$eval("#trg-f-source option", (els) => els.map((e) => e.value));
      check(JSON.stringify(opts) === JSON.stringify(["", "email.received@1", "feed.item@1", "webhook.received@1"]), "create: only sources that need a connection", opts);
      await page.selectOption("#trg-f-source", "feed.item@1");
      await page.waitForSelector("#trg-f-url");
      check((await page.inputValue("#trg-f-every_minutes")) === "15", "create: schema default prefilled");
      await page.fill("#trg-f-label", "Changelog");
      await page.fill("#trg-f-url", "not a url");
      await page.click("[data-trg-create]");
      check((await page.textContent("#trigger-modal-note")).includes("Feed address does not have the expected form."), "create: the schema's pattern is checked");
      await page.fill("#trg-f-url", "https://example.org/changelog.xml");
      await shotEl(page, "#trigger-modal-backdrop .af-modal", "triggers-create-feed-1440-light");
      await page.click("[data-trg-create]");
      await page.waitForSelector("[data-trg-done]");
      check(JSON.stringify(store.lastCreate) === JSON.stringify({ source_id: "feed.item", source_version: 1, label: "Changelog", config: { url: "https://example.org/changelog.xml", every_minutes: 15 } }), "create: the POST body", store.lastCreate);
      check(await page.locator("[data-trg-value='created:secret']").count() === 0, "create: no secret for a feed");
      await page.click("[data-trg-done]");
      await page.waitForSelector('#triggers-table tr.trg-row:has-text("Changelog")');
      // Create a webhook: the address and the one-time secret, each with a copy button.
      await page.click("#triggers-new");
      await page.waitForSelector("#trg-f-source");
      await page.selectOption("#trg-f-source", "webhook.received@1");
      await page.fill("#trg-f-label", "GitHub pushes");
      await page.click("[data-trg-create]");
      await page.waitForSelector("[data-trg-value='created:secret']");
      check((await page.textContent("[data-trg-value='created:secret']")).trim() === "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw", "webhook: secret shown");
      check((await page.textContent("#trigger-modal-body")).includes("shown once"), "webhook: shown-once sentence");
      check((await page.textContent("[data-trg-value='created:ingress']")).includes("/api/gateway/triggers/webhooks/conn_new4"), "webhook: ingress address");
      await page.click("[data-trg-copy='created:secret']");
      await page.waitForTimeout(200);
      const clip = await page.evaluate(() => navigator.clipboard.readText().catch(() => "unreadable"));
      check(clip === "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw" || clip === "unreadable", "webhook: copy puts the secret on the clipboard", clip);
      await shotEl(page, "#trigger-modal-backdrop .af-modal", "triggers-webhook-secret-once-1440-light");
      await page.click("[data-trg-done]");
      // The secret is gone once the dialog closes: the row's detail shows the address only.
      const hook = page.locator('#triggers-table tr.trg-row:has-text("GitHub pushes")');
      await hook.locator("[data-trg-events]").click();
      await page.waitForSelector('[data-trg-detail="conn_new4"]');
      check(!(await page.textContent('[data-trg-detail="conn_new4"]')).includes("whsec_"), "webhook: the secret is never shown again");
      check((await page.textContent('[data-trg-detail="conn_new4"]')).includes("Webhook address"), "webhook: address in the row detail");
      // Delete with confirmation (Cancel first, then Delete).
      await hook.locator("[data-trg-delete]").click();
      await page.waitForSelector("#triggers-table .row-confirm");
      check((await page.textContent("#triggers-table .row-confirm")).includes("Delete GitHub pushes? Automations that listen to it stop receiving its events."), "delete: the question");
      await shot(page, "triggers-delete-confirm-1440-light");
      await page.click("#triggers-table .row-confirm button.secondary");
      check(await page.locator("#triggers-table .row-confirm").count() === 0, "delete: Cancel keeps it");
      await hook.locator("[data-trg-delete]").click();
      await page.click("#triggers-table .row-confirm button.danger");
      await page.waitForFunction(() => !document.querySelector('[data-trg-row="conn_new4"]'));
      check(store.requests.includes("DELETE /api/gateway/triggers/connections/conn_new4"), "delete: DELETE sent");
      check((await page.textContent("#triggers-message")).includes("Deleted GitHub pushes."), "delete: said");
      // Admin: All accounts shows the Account column and other people's connections.
      await page.click("#triggers-all");
      await page.waitForSelector('[data-trg-row="conn_bob1"]');
      check(store.requests.includes("GET /api/gateway/triggers/connections?all=true"), "all accounts: ?all=true");
      check(await page.locator("#triggers-account-head").isVisible(), "all accounts: Account column");
      check((await page.textContent('[data-trg-row="conn_bob1"]')).includes("bob"), "all accounts: owner shown");
      await shot(page, "triggers-all-accounts-1440-light");
    }
    await ctx.close();
  }
  // Deep link: `#triggers?new=feed.item` opens the dialog on that source.
  {
    const { ctx, page } = await newPage(browser, { width: 1280, height: 860 });
    await signIn(page, "admin", ADMIN, "#triggers?new=feed.item");
    await page.waitForSelector("#trigger-modal-backdrop:not([hidden]) #trg-f-url", { timeout: 30000 });
    check((await page.inputValue("#trg-f-source")) === "feed.item@1", "deep link: source preselected");
    await ctx.close();
  }
} finally {
  await browser.close();
}
console.log(JSON.stringify({ failures, checks, shots }));
