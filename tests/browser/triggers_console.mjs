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
import crypto from "node:crypto";
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
    { id: "email.received", version: 1, label: "When an email arrives", available: true, needs_connection: true, connection_provider: "email_accounts", config_schema: { type: "object" }, event_schema: {}, capabilities: { kind: "event" },
      connection_schema: { type: "object", properties: { account: { type: "string", default: "self", description: "the mailbox: a mail account id of the user ('self': the primary mailbox)" } } } },
    { id: "feed.item", version: 1, label: "When a feed has a new item", available: true, needs_connection: true, config_schema: { type: "object" }, event_schema: {}, capabilities: { kind: "event" },
      connection_schema: { type: "object", required: ["url"], properties: {
        url: { type: "string", title: "Feed address", description: "An RSS or Atom address.", pattern: "^https?://", examples: ["https://example.org/feed.xml"] },
        every_minutes: { type: "integer", title: "Check every (minutes)", default: 15, minimum: 5 } } } },
    { id: "webhook.received", version: 1, label: "When a webhook is called", available: true, needs_connection: true, config_schema: { type: "object" }, event_schema: {}, capabilities: { kind: "event" },
      connection_schema: { type: "object", additionalProperties: false, properties: {
        subject_path: { type: "string", description: "dotted path in a JSON body for the event subject (default: the webhook id)" },
        tolerance_s: { type: "integer", minimum: 60, maximum: 3600, default: 300 },
        secret: { type: "string", "x-secret": true, description: "whsec_<base64>; generated when omitted" } } } },
  ],
};

// The fake store behind the contract routes (per browser context).
function fakeStore() {
  const t0 = Date.now();
  const iso = (msAgo) => new Date(t0 - msAgo).toISOString();
  const s = {
    seq: 3, n: 2,
    rows: [
      { connection_id: "conn_feed1", source_id: "feed.item", source_version: 1, label: "Release notes feed", status: "active", created_at: iso(86400000), last_event_at: iso(240000), config_public: { url: "https://example.org/releases.xml", every_minutes: 15 }, principal: "default:admin", paused: false, always_listen: false, listening: "listening", used_by: 2 },
      { connection_id: "conn_mail1", source_id: "email.received", source_version: 1, label: "My mailbox", status: "active", created_at: iso(86400000 * 3), last_event_at: null, config_public: { account: "self" }, principal: "default:admin", paused: false, always_listen: false, listening: "idle", used_by: 0 },
    ],
    others: [
      { connection_id: "conn_bob1", source_id: "webhook.received", source_version: 1, label: "Bob's CI hook", status: "paused", created_at: iso(3600000), last_event_at: iso(3600000), config_public: {}, ingress_url: `${BASE}/api/gateway/triggers/webhooks/conn_bob1`, principal: "default:bob" },
    ],
    events: {
      conn_feed1: [1, 2, 3].map((i) => ({ specversion: "1.0", id: `evt_feed_${i}`, type: "abstractframework.feed.item", source: "trigger:conn_feed1", subject: `Release 0.1${i}`, time: iso(240000 + (3 - i) * 3600000), datacontenttype: "application/json",
        data: { title: `Release 0.1${i}`, link: `https://example.org/releases/0.1${i}` }, connectionid: "conn_feed1", sequence: i, dedupekey: `guid-${i}`, abstracttrust: "external" })),
    },
    requests: [],
    mailboxes: [
      { account_id: "default", primary: true, configured: true, address: "admin@example.org", can_read: true, can_send: true, enabled: true, folder: "INBOX", mailbox: { state: "connected" }, watcher: { state: "idle", last_poll: iso(120000) } },
    ],
  };
  return s;
}

const SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw";
async function mockRoutes(ctx, store) {
  // The signed ingress: verified here exactly as Standard Webhooks says (the gateway's own check).
  await ctx.route("**/api/gateway/triggers/webhooks/**", async (route) => {
    const req = route.request();
    const h = req.headers();
    const id = h["webhook-id"], ts = h["webhook-timestamp"], sig = String(h["webhook-signature"] || "");
    const body = req.postData() || "";
    store.requests.push(`INGRESS ${new URL(req.url()).pathname}`);
    const want = "v1," + crypto.createHmac("sha256", Buffer.from(SECRET.slice(6), "base64")).update(`${id}.${ts}.${body}`).digest("base64");
    if (!id || !ts || sig !== want) return route.fulfill({ status: 401, json: { detail: { reason_code: "webhook_signature_invalid", message: "bad signature" } } });
    const cid = new URL(req.url()).pathname.split("/").pop();
    store.seq += 1;
    const ev = { specversion: "1.0", id: `evt_hook_${store.seq}`, type: "abstract.webhook.received.v1", source: `abstract://triggers/connections/${cid}`, subject: id, time: new Date().toISOString(),
      data: { webhook_id: id, content_type: "application/json", body: JSON.parse(body) }, connectionid: cid, sequence: store.seq, abstracttrust: "untrusted" };
    (store.events[cid] = store.events[cid] || []).push(ev);
    return route.fulfill({ status: 202, json: { accepted: true, event_id: ev.id, seq: ev.sequence, woken: 0 } });
  });
  await ctx.route("**/api/gateway/me/email/oauth/clients", (route) => route.fulfill({ json: { providers: {
    google: { configured: false, client_id: "", client_secret_set: false, tenant: "", builtin_available: true, needs_reconnect: null },
    microsoft: { configured: false, client_id: "", client_secret_set: false, tenant: "", builtin_available: false, needs_reconnect: null } } } }));
  // Further mailboxes (Account → My email): never a real IMAP sign-in in this test.
  await ctx.route("**/api/gateway/me/email/accounts**", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const parts = url.pathname.split("/").filter(Boolean); // api gateway me email accounts [id] [test|enabled]
    const id = parts[5], sub = parts[6], method = req.method();
    store.requests.push(`${method} ${url.pathname}`);
    if (!id) return route.fulfill({ json: { items: store.mailboxes } });
    if (method === "PUT" && !sub) {
      const b = JSON.parse(req.postData() || "{}");
      store.lastMailboxPut = { id, body: b };
      if (b.imap && b.imap.host === "imap.gmail.com") return route.fulfill({ status: 400, json: { detail: { reason_code: "email_auth_failed", message: "IMAP sign-in failed: [AUTHENTICATIONFAILED] Invalid credentials" } } });
      const row = { account_id: id, primary: false, configured: true, address: b.address, can_read: true, can_send: false, enabled: true, folder: "INBOX", mailbox: { state: "receive_only" }, watcher: { state: "idle" } };
      store.mailboxes = store.mailboxes.filter((m) => m.account_id !== id).concat([row]);
      return route.fulfill({ json: { ok: true, ...row } });
    }
    const m = store.mailboxes.find((x) => x.account_id === id);
    if (!m) return route.fulfill({ status: 404, json: { detail: { message: "No such mailbox." } } });
    if (sub === "enabled") { m.enabled = !!JSON.parse(req.postData() || "{}").enabled; return route.fulfill({ json: { ok: true, ...m } }); }
    if (sub === "test") return route.fulfill({ json: { ok: true } });
    if (method === "DELETE") { store.mailboxes = store.mailboxes.filter((x) => x.account_id !== id); return route.fulfill({ json: { ok: true } }); }
    return route.fulfill({ status: 405, json: {} });
  });
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
      return route.fulfill({ json: Object.assign({}, row, body.source_id === "webhook.received" ? { secret: SECRET } : {}) });
    }
    const row = store.rows.concat(store.others).find((r) => r.connection_id === id);
    if (!row) return route.fulfill({ status: 404, json: { detail: { message: "No such connection." } } });
    if (method === "PUT" && !sub) {
      const b = JSON.parse(req.postData() || "{}");
      store.lastConnPut = { id, body: b };
      if (typeof b.paused === "boolean") row.paused = b.paused;
      if (typeof b.always_listen === "boolean") row.always_listen = b.always_listen;
      row.listening = row.paused ? "paused" : ((row.used_by || 0) > 0 || row.always_listen ? "listening" : "idle");
      return route.fulfill({ json: row });
    }
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
    check((await feedRow.textContent()).includes("When Release notes feed has a new item") && !(await feedRow.textContent()).includes("feed.item"), `${tag}: the trigger in words, never an id`);
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
      check((await page.textContent('[data-trg-row="conn_feed1"]')).includes("Listening · used by 2 automations"), "row: listening in words");
      check((await page.textContent('[data-trg-row="conn_mail1"]')).includes("Listening · nothing uses it yet, so nothing is fetched until an automation does"), "row: pressed + unused in words");
      await page.click('[data-trg-row="conn_mail1"] .trg-advanced summary');
      await page.click('[data-trg-always="conn_mail1"]');
      await page.waitForFunction(() => (document.querySelector('[data-trg-row="conn_mail1"]') || {}).textContent.includes("fetching anyway"));
      check(JSON.stringify(store.lastConnPut) === JSON.stringify({ id: "conn_mail1", body: { always_listen: true } }), "Keep fetching: PUT {always_listen: true}", store.lastConnPut);
      // The mailbox row: the same states, with the mailbox's own switch.
      await page.click('[data-trg-mailbox-use="default"]');
      await page.waitForFunction(() => (document.querySelector('[data-trg-mailbox="default"]') || {}).textContent.includes("Paused"));
      check(store.requests.includes("PUT /api/gateway/me/email/accounts/default/enabled"), "mailbox row switch: PUT enabled");
      await page.click('[data-trg-mailbox-use="default"]');
      await page.waitForFunction(() => !(document.querySelector('[data-trg-mailbox="default"]') || {}).textContent.includes("Paused"));
      check((await page.textContent("#tab-triggers")).includes("A connection only fetches while an automation uses it; pause it to stop even then."), "help line under the list");
      await page.click('[data-trg-listen="conn_feed1"]');
      await page.waitForFunction(() => (document.querySelector('[data-trg-row="conn_feed1"]') || {}).textContent.includes("Paused"));
      check(JSON.stringify(store.lastConnPut) === JSON.stringify({ id: "conn_feed1", body: { paused: true } }), "Listening toggle: PUT {paused: true}", store.lastConnPut);
      check((await page.getAttribute('[data-trg-listen="conn_feed1"]', "aria-checked")) === "false", "Listening toggle shows its state");
      await shot(page, "triggers-listening-1440-light");
      await page.click('[data-trg-listen="conn_feed1"]');
      await page.waitForFunction(() => (document.querySelector('[data-trg-row="conn_feed1"]') || {}).textContent.includes("Listening · used by 2"));
      check(await page.locator('[data-trg-mailbox="default"]').count() === 1, "mailbox row listed");
      check((await page.textContent('[data-trg-mailbox="default"]')).includes("When mail arrives in admin@example.org"), "mailbox row: the trigger in words");
      check((await page.textContent('[data-trg-mailbox="default"]')).includes("Manage under Account → My email"), "mailbox row: link");
      await page.click("#triggers-new");
      await page.waitForSelector("#trigger-modal-backdrop:not([hidden]) [data-trg-source]");
      const opts = await page.$$eval("#trg-f-source option", (els) => els.map((e) => e.value));
      check(JSON.stringify(opts) === JSON.stringify(["", "feed.item@1", "webhook.received@1"]), "create: connection sources only, never the mailbox source", opts);
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
      await page.waitForSelector("#trg-f-tolerance_s");
      const form = await page.textContent("#trigger-modal-body");
      check(form.includes("Subject (path in the JSON body)") && form.includes("Accepted clock skew (seconds)"), "webhook form: human labels", form);
      check(!form.includes("subject_path") && !form.includes("Not editable here") && await page.locator("#trg-f-secret").count() === 0, "webhook form: no raw keys, no secret field");
      await shotEl(page, "#trigger-modal-backdrop .af-modal", "triggers-create-webhook-1440-light");
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
      const curl = await page.textContent("[data-trg-value='created:curl']");
      check(curl.includes(`SECRET='${SECRET}'`) && curl.includes("/api/gateway/triggers/webhooks/conn_new4") && curl.includes("openssl dgst -sha256 -mac HMAC"), "setup: signed call with the real address and secret", curl);
      check((await page.textContent("#trigger-modal-body")).includes("paste the webhook address as the webhook URL"), "setup: GitHub/Zapier recipe");
      await page.click("[data-trg-hook-test]");
      await page.waitForSelector("[data-trg-hook-result] .trg-fire");
      const result = await page.textContent("[data-trg-hook-result]");
      check(result.includes("HTTP 202") && result.includes("The event it created"), "signed test call: 202 and the event", result);
      check(store.requests.some((r) => r === "INGRESS /api/gateway/triggers/webhooks/conn_new4"), "signed test call reached the ingress");
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
      await page.click("#triggers-all");
      // Manage under Account → My email: the further-mailboxes card, add lpalbou@abstractframework.ai read-only.
      await page.waitForSelector('[data-trg-mailbox="default"]');
      await page.click('[data-trg-mailbox-manage="default"]');
      await page.waitForSelector("#my-mailboxes-card:not([hidden])", { timeout: 30000 });
      await page.click("#my-mailbox-add");
      check(await page.locator("#my-mailbox-google:not([hidden])").count() === 1, "Sign in with Google offered (built-in client)");
      // Gmail: provider-aware help, before and after a refused sign-in.
      await page.fill("#my-mailbox-id", "personal");
      await page.fill("#my-mailbox-address", "someone@gmail.com");
      await page.fill("#my-mailbox-imap-host", "imap.gmail.com");
      await page.fill("#my-mailbox-password", "account-password");
      check((await page.textContent("#my-mailbox-provider-help")).startsWith("Gmail refuses account passwords."), "Gmail help shown while typing");
      await page.click("#my-mailbox-add-go");
      await page.waitForSelector("#my-mailbox-error:not([hidden])");
      check((await page.textContent("#my-mailbox-error")) === "Not added. Gmail refuses account passwords. Create an app password and paste it here, or Sign in with Google.", "Gmail refusal: provider-aware help", await page.textContent("#my-mailbox-error"));
      const link = page.locator('#my-mailbox-error a');
      check((await link.getAttribute("href")) === "https://myaccount.google.com/apppasswords" && (await link.getAttribute("target")) === "_blank", "Gmail refusal: a direct link in a new tab");
      await shotEl(page, "#my-mailboxes-card", "my-email-gmail-help-1440-light");
      await page.fill("#my-mailbox-id", "abstractframework");
      await page.fill("#my-mailbox-address", "lpalbou@abstractframework.ai");
      await page.fill("#my-mailbox-imap-host", "imap.abstractframework.ai");
      await page.fill("#my-mailbox-imap-port", "993");
      await page.fill("#my-mailbox-password", "app-password");
      await shotEl(page, "#my-mailboxes-card", "my-email-add-mailbox-1440-light");
      await page.click("#my-mailbox-add-go");
      await page.waitForSelector('[data-mbx-row="abstractframework"]');
      check(JSON.stringify(store.lastMailboxPut) === JSON.stringify({ id: "abstractframework", body: { address: "lpalbou@abstractframework.ai", password: "app-password", username: "", imap: { host: "imap.abstractframework.ai", security: "ssl", port: 993 }, send: false, test: true } }), "add mailbox: read-only PUT body", store.lastMailboxPut);
      check((await page.textContent('[data-mbx-row="abstractframework"]')).includes("read-only"), "mailbox row: read-only");
      await page.click('[data-mbx-row="abstractframework"] [data-mbx-test]');
      await page.waitForSelector('[data-mbx-row="abstractframework"] .mbx-row__state.is-ok');
      const sw = page.locator('#my-mailbox-enabled-abstractframework');
      check((await sw.getAttribute("role")) === "switch" && (await sw.textContent()).includes("Use this mailbox"), "mailbox switch: feature-labelled");
      await sw.click();
      await page.waitForFunction(() => document.getElementById("my-mailbox-enabled-abstractframework") && document.getElementById("my-mailbox-enabled-abstractframework").getAttribute("aria-checked") === "false");
      check(store.requests.includes("PUT /api/gateway/me/email/accounts/abstractframework/enabled"), "mailbox switch: PUT enabled");
      await shotEl(page, "#my-mailboxes-card", "my-email-further-mailboxes-1440-light");
      await page.click('[data-mbx-row="abstractframework"] [data-mbx-delete]');
      check((await page.textContent('[data-mbx-row="abstractframework"]')).includes("Delete the mailbox abstractframework (lpalbou@abstractframework.ai)?"), "mailbox delete: asks first");
      await page.click('[data-mbx-delete-now="abstractframework"]');
      await page.waitForFunction(() => !document.querySelector('[data-mbx-row="abstractframework"]'));
      check(store.requests.includes("DELETE /api/gateway/me/email/accounts/abstractframework"), "mailbox delete: DELETE");
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
