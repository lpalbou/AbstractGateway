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
        // The runtime's own feed settings (abstractruntime triggers/feed.py), verbatim.
        every: { type: "string", format: "duration", pattern: "^[1-9][0-9]*[smhd]$", default: "15m", title: "Check every",
          description: "How often to check the feed: a number and a unit (s, m, h or d), for example 15m. At least 60s." },
        max_items: { type: "integer", minimum: 1, maximum: 50, default: 20, title: "Most new items per check", description: "The most new items one check delivers; older new items are skipped." },
        initial_items: { type: "string", enum: ["skip", "deliver"], enumNames: ["Ignore what is already there", "Also deliver the latest items"], default: "skip", title: "When added",
          description: "What to do with the items already in the feed when it is first checked: ignore what is already there (skip), or also deliver the latest items (deliver)." } } } },
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
      { account_id: "default", primary: true, configured: true, address: "admin@example.org", can_read: true, can_send: true, can_write: true, access: "write", enabled: true, folder: "INBOX", mailbox: { state: "connected" }, watcher: { state: "idle", last_poll: iso(120000) } },
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
  // My email: the primary connected (over the real answer), and the provider lookup by domain.
  await ctx.route((u) => u.pathname === "/api/gateway/me/email", async (route) => {
    if (route.request().method() !== "GET") return route.fulfill({ status: 405, json: {} });
    const real = await (await route.fetch()).json();
    return route.fulfill({ json: { ...real, configured: true, address: "admin@example.org", email_address: "admin@example.org", enabled: true, auth_kind: "password", username: "",
      imap: { host: "imap.example.org", port: 993, security: "ssl", folder: "INBOX" }, smtp: { host: "smtp.example.org", port: 465, security: "ssl" },
      mailbox: { state: "connected", address: "admin@example.org", provider: "imap", reason: null } } });
  });
  await ctx.route((u) => u.pathname === "/api/gateway/me/email/autodiscover", (route) => {
    const address = new URL(route.request().url()).searchParams.get("address").toLowerCase();
    store.requests.push(`GET autodiscover ${address}`);
    const domain = address.split("@")[1];
    const legs = (i, o, sec = "ssl", port = 465) => ({ imap: { host: i, port: 993, security: "ssl" }, smtp: o ? { host: o, port, security: sec } : null });
    const known = {
      "gmail.com": { provider: "google", display_name: "Gmail", ...legs("imap.gmail.com", "smtp.gmail.com"), sign_in: ["oauth", "app_password"], oauth_available: true, app_password_url: "https://myaccount.google.com/apppasswords", source: "preset" },
      "fastmail.com": { provider: "fastmail", display_name: "Fastmail", ...legs("imap.fastmail.com", "smtp.fastmail.com"), sign_in: ["app_password"], oauth_available: false, source: "preset" },
      "outlook.com": { provider: "microsoft", display_name: "Outlook / Microsoft 365", ...legs("outlook.office365.com", "smtp.office365.com", "starttls", 587), sign_in: ["oauth"], oauth_available: false, source: "preset" },
    }[domain] || { provider: "other", display_name: domain, imap: { host: "", port: 993, security: "ssl" }, smtp: null, sign_in: ["password"], oauth_available: false, source: "none" };
    return route.fulfill({ json: { address, login: address, ...known } });
  });
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
      const row = { account_id: id, primary: false, configured: true, address: b.address, name: b.name, can_read: true, can_send: !!b.write, can_write: !!b.write, access: b.write ? "write" : "read", enabled: true, folder: "INBOX", mailbox: { state: "connected", provider: "imap" }, watcher: { state: "idle" } };
      store.mailboxes = store.mailboxes.filter((m) => m.account_id !== id).concat([row]);
      return route.fulfill({ json: { ok: true, ...row } });
    }
    const m = store.mailboxes.find((x) => x.account_id === id);
    if (!m) return route.fulfill({ status: 404, json: { detail: { message: "No such mailbox." } } });
    if (sub === "enabled") { m.enabled = !!JSON.parse(req.postData() || "{}").enabled; return route.fulfill({ json: { ok: true, ...m } }); }
    if (sub === "access") { const w = !!JSON.parse(req.postData() || "{}").write; store.lastAccessPut = { id, write: w }; Object.assign(m, { access: w ? "write" : "read", can_write: w, can_send: w }); return route.fulfill({ json: { ok: true, ...m } }); }
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
    const ids = await page.$$eval('[data-trg-detail="conn_feed1"] [data-trg-event]', (els) => els.map((e) => e.dataset.trgEvent));
    check(JSON.stringify(ids) === JSON.stringify(["evt_feed_3", "evt_feed_2", "evt_feed_1"]), `${tag}: events newest first`, ids);
    check((await page.textContent('[data-trg-event="evt_feed_3"] .trg-event__line')).startsWith("New item: Release 0.13 · "), `${tag}: an event reads as a sentence`);
    await page.locator('[data-trg-event="evt_feed_3"] summary').click();
    check((await page.locator('[data-trg-event="evt_feed_3"] pre').textContent()).includes('"title": "Release 0.13"'), `${tag}: event JSON expands`);
    // Test: a visible result in the row, and the event appears in the expanded list.
    await feedRow.locator("[data-trg-test]").click();
    await page.waitForSelector('[data-trg-row="conn_feed1"] .trg-fire.is-ok');
    check((await page.locator('[data-trg-row="conn_feed1"] .trg-fire').textContent()).trim() === "Test event #4 sent.", `${tag}: test result shown`);
    await page.waitForSelector('[data-trg-event="evt_test_4"]');
    check((await page.locator('[data-trg-event="evt_test_4"] .trg-event__line').textContent()).startsWith("Test event · "), `${tag}: test event listed in words`);
    check((await page.locator('[data-trg-row="conn_feed1"] td.trg-last').textContent()).trim() === "just now" || w < 1024, `${tag}: last event refreshed`);
    if (w === 1440) await shot(page, `triggers-list-events-${tag}`);
    else await shot(page, `triggers-list-${tag}`);

    if (w === 1440 && theme === "light") {
      // Create a feed connection: the form comes from connection_schema.
      check((await page.textContent('[data-trg-row="conn_feed1"]')).includes("Listening · used by 2 automations"), "row: listening in words");
      check((await page.textContent('[data-trg-row="conn_mail1"]')).includes("Waiting for an automation to use it"), "row: pressed + unused in words");
      await page.click('[data-trg-row="conn_mail1"] .trg-advanced summary');
      await page.click('[data-trg-always="conn_mail1"]');
      await page.waitForFunction(() => (document.querySelector('[data-trg-row="conn_mail1"]') || {}).textContent.includes("kept on even when nothing uses it"));
      check(JSON.stringify(store.lastConnPut) === JSON.stringify({ id: "conn_mail1", body: { always_listen: true } }), "Keep fetching: PUT {always_listen: true}", store.lastConnPut);
      // The mailbox row: the same states, with the mailbox's own switch.
      await page.click('[data-trg-mailbox-use="default"]');
      await page.waitForFunction(() => (document.querySelector('[data-trg-mailbox="default"]') || {}).textContent.includes("Paused"));
      check(store.requests.includes("PUT /api/gateway/me/email/accounts/default/enabled"), "mailbox row switch: PUT enabled");
      await page.click('[data-trg-mailbox-use="default"]');
      await page.waitForFunction(() => !(document.querySelector('[data-trg-mailbox="default"]') || {}).textContent.includes("Paused"));
      check((await page.textContent("#tab-triggers")).includes("A connection only fetches while an automation uses it. Pause it to stop fetching."), "help line under the list");
      await page.click('[data-trg-listen="conn_feed1"]');
      await page.waitForFunction(() => (document.querySelector('[data-trg-row="conn_feed1"]') || {}).textContent.includes("Paused"));
      check(JSON.stringify(store.lastConnPut) === JSON.stringify({ id: "conn_feed1", body: { paused: true } }), "Listening toggle: PUT {paused: true}", store.lastConnPut);
      check((await page.getAttribute('[data-trg-listen="conn_feed1"]', "aria-checked")) === "false", "Listening toggle shows its state");
      await shot(page, "triggers-listening-1440-light");
      await page.click('[data-trg-listen="conn_feed1"]');
      await page.waitForFunction(() => (document.querySelector('[data-trg-row="conn_feed1"]') || {}).textContent.includes("Listening · used by 2"));
      check(await page.locator('[data-trg-mailbox="default"]').count() === 1, "mailbox row listed");
      check((await page.textContent('[data-trg-mailbox="default"]')).includes("When mail arrives in admin@example.org"), "mailbox row: the trigger in words");
      check((await page.textContent('[data-trg-mailbox="default"]')).includes("Manage in My email"), "mailbox row: link");
      await page.click("#triggers-new");
      await page.waitForSelector("#trigger-modal-backdrop:not([hidden]) [data-trg-source]");
      const opts = await page.$$eval("#trg-f-source option", (els) => els.map((e) => e.value));
      check(JSON.stringify(opts) === JSON.stringify(["", "feed.item@1", "webhook.received@1"]), "create: connection sources only, never the mailbox source", opts);
      await page.selectOption("#trg-f-source", "feed.item@1");
      await page.waitForSelector("#trg-f-url");
      check((await page.inputValue("#trg-f-every")) === "15" && (await page.$eval('[data-trg-dur-u="every"]', (el) => el.selectedOptions[0].textContent)) === "minutes", "create: Check every [15] [minutes]");
      const whenAdded = await page.$$eval("#trg-f-initial_items option", (els) => els.map((e) => [e.textContent, e.selected]));
      check(JSON.stringify(whenAdded) === JSON.stringify([["Ignore what is already there", true], ["Also deliver the latest items", false]]), "create: When added is a choice in words", whenAdded);
      const feedForm = await page.textContent("#trigger-modal-body");
      check(!feedForm.includes("Not editable here") && !feedForm.includes("enumNames") && !feedForm.includes("(skip)") && !feedForm.includes("s, m, h or d"), "create: no raw JSON, no restated values", feedForm);
      // The well-known feeds (the gateway's own GET /triggers/feeds/catalog, not routed here).
      await page.waitForSelector("#trg-f-catalog optgroup", { state: "attached" });
      const groups = await page.$$eval("#trg-f-catalog optgroup", (els) => els.map((e) => e.label));
      check(["World news", "Technology", "Science", "Business and finance", "Fill in your own"].every((g) => groups.includes(g)), "create: well-known feeds grouped by category", groups);
      const labels = await page.$$eval("#trigger-modal-body .trg-field > label", (els) => els.map((e) => e.textContent));
      check(labels.indexOf("Choose a well-known feed") >= 0 && labels.indexOf("Choose a well-known feed") < labels.indexOf("Feed address *"), "create: the picker comes before the address", labels);
      await page.selectOption("#trg-f-catalog", "bbc-world");
      check((await page.inputValue("#trg-f-label")) === "BBC News · World" && (await page.inputValue("#trg-f-url")) === "https://feeds.bbci.co.uk/news/world/rss.xml", "pick: the name and the address fill in");
      check((await page.textContent("[data-trg-catalog-about]")).includes("bbc.co.uk"), "pick: the site is named");
      await shotEl(page, "#trigger-modal-backdrop .af-modal", "triggers-create-feed-picked-1440-light");
      await page.selectOption("#trg-f-catalog", "github-releases");
      await page.waitForSelector("#trg-f-feed-param");
      await page.type("#trg-f-feed-param", "python/cpython");
      check((await page.inputValue("#trg-f-url")) === "https://github.com/python/cpython/releases.atom" && (await page.inputValue("#trg-f-label")) === "GitHub · python/cpython releases", "template: the value builds the address and the name");
      await shotEl(page, "#trigger-modal-backdrop .af-modal", "triggers-create-feed-template-1440-light");
      await page.fill("#trg-f-label", "CPython releases");
      await page.selectOption("#trg-f-catalog", "__another");
      check((await page.inputValue("#trg-f-url")) === "" && (await page.inputValue("#trg-f-label")) === "CPython releases" && (await page.locator("#trg-f-feed-param").count()) === 0, "Another address: the free field, the person's name kept");
      await page.selectOption("#trg-f-initial_items", { label: "Also deliver the latest items" });
      await page.fill("#trg-f-label", "Changelog");
      await page.fill("#trg-f-url", "not a url");
      await page.click("[data-trg-create]");
      await page.waitForSelector("#trigger-modal-body .trg-field-error");
      check((await page.textContent("#trigger-modal-body .trg-field-error")).startsWith("Feed address: start with https://"), "create: the refusal beside the field, in red");
      check((await page.inputValue("#trg-f-url")) === "not a url", "create: the text is kept");
      await page.fill("#trg-f-url", "https://example.org/changelog.xml");
      await shotEl(page, "#trigger-modal-backdrop .af-modal", "triggers-create-feed-1440-light");
      await page.click("[data-trg-create]");
      await page.waitForSelector("[data-trg-done]");
      check((await page.textContent("#trigger-modal-body")).includes("Next: in Flow, + → When something arrives → Changelog."), "created: the next step");
      check(JSON.stringify(store.lastCreate) === JSON.stringify({ source_id: "feed.item", source_version: 1, label: "Changelog", config: { url: "https://example.org/changelog.xml", every: "15m", max_items: 20, initial_items: "deliver" } }), "create: the POST body", store.lastCreate);
      check(await page.locator("[data-trg-value='created:secret']").count() === 0, "create: no secret for a feed");
      await page.click("[data-trg-done]");
      await page.waitForSelector('#triggers-table tr.trg-row:has-text("Changelog")');
      // Create a webhook: the address and the one-time secret, each with a copy button.
      await page.click("#triggers-new");
      await page.waitForSelector("#trg-f-source");
      await page.selectOption("#trg-f-source", "webhook.received@1");
      await page.waitForSelector("#trigger-modal-body details.trg-more");
      check(await page.locator("#trigger-modal-body details.trg-more[open]").count() === 0, "webhook form: Name only, the rest collapsed");
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
      check((await page.textContent("#trigger-modal-body")).includes("GitHub can't reach this computer yet. Turn on internet access under Network first."), "setup: a local address says GitHub can't reach it");
      await page.click("[data-trg-hook-test]");
      await page.waitForSelector("[data-trg-hook-result] .trg-fire");
      const result = await page.textContent("[data-trg-hook-result]");
      check(result.includes("It worked: GitHub pushes received the call · see it under Events.") && !result.includes("fetch"), "signed test call: it worked, in words", result);
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
      // Manage → My email: a compact header, tabs Mailboxes | Notifications | Agents, each fits at 1440×900.
      await page.waitForSelector('[data-trg-mailbox="default"]');
      await page.click('[data-trg-mailbox-manage="default"]');
      await page.waitForSelector('#my-mailboxes-list [data-mbx-row="__primary"]', { timeout: 30000 });
      check((await page.textContent("#account-email-title")).trim() === "My email", "my email: title");
      check(await page.locator("#account-email-body .account-modal-lead, #account-email-body .af-callout").count() === 0, "my email: no banner");
      const tabs = await page.$$eval(".my-email-tabs [role=tab]", (els) => els.map((e) => e.textContent.trim()));
      check(JSON.stringify(tabs) === JSON.stringify(["Mailboxes", "Notifications", "Agents"]), "my email: three tabs", tabs);
      const fits = () => page.evaluate(() => { const b = document.getElementById("account-email-body"); return b.scrollHeight - b.clientHeight; });
      for (const t of ["mailboxes", "notifications", "agents"]) {
        await page.click(`#my-email-sec-${t}`);
        await page.waitForSelector(`#my-email-pane-${t}:not([hidden])`);
        await page.waitForTimeout(150);
        check((await fits()) <= 1, `my email: ${t} fits without scrolling at 1440×900`, await fits());
        await shotEl(page, "#account-email-backdrop .af-modal", `my-email-tab-${t}-1440-light`);
      }
      await page.click("#my-email-sec-mailboxes");
      const primaryRow = (await page.textContent('[data-mbx-row="__primary"]')).replace(/\s+/g, " ");
      check(primaryRow.includes("admin@example.org · connected") && primaryRow.includes("Sends as you"), "mailboxes: primary row one line + badge", primaryRow);
      check(!(await page.textContent("#my-mailboxes-list")).includes("INBOX"), "mailboxes: no folder repeated");
      // Add mailbox: the form in a dialog; step 1 is one field.
      await page.click("#my-mailbox-add");
      await page.waitForSelector("#mailbox-sheet-backdrop:not([hidden]) #mbs-address");
      check((await page.textContent("#mailbox-sheet-title")).trim() === "Add mailbox", "form: title");
      check(await page.evaluate(() => document.activeElement && document.activeElement.id) === "mbs-address", "form: Email address has focus");
      check(await page.locator("#mailbox-sheet-body input").count() === 1 && await page.locator("#mailbox-sheet-go").isHidden(), "form: step 1 is one field, no action yet");
      await shotEl(page, "#mailbox-sheet-backdrop .af-modal", "mailbox-form-empty-1440-light");
      // Gmail: found, Sign in with Google first.
      await page.fill("#mbs-address", "someone@gmail.com");
      await page.waitForSelector('[data-mbs-oauth="google"]');
      check((await page.textContent("[data-mbs-found]")).trim() === "Gmail · Sign in with Google, or use an app password", "form: Gmail found line");
      check(await page.locator("#mbs-password").count() === 0 && await page.locator("details.mbs-servers[open]").count() === 0, "form: Gmail — sign-in, no password, servers collapsed");
      await shotEl(page, "#mailbox-sheet-backdrop .af-modal", "mailbox-form-gmail-1440-light");
      // The app-password way, refused: the error inline in red, the text kept, the direct link.
      await page.click("[data-mbs-use-password]");
      await page.fill("#mbs-password", "account-password");
      await page.click("#mailbox-sheet-go");
      await page.waitForSelector('#mailbox-sheet-body .mbs-error[role="alert"]');
      check((await page.textContent('#mailbox-sheet-body .mbs-error[role="alert"]')) === "Not connected: Gmail refused this password. Use an app password (the link above), or Sign in with Google.", "form: Gmail refusal, one short sentence", await page.textContent('#mailbox-sheet-body .mbs-error[role="alert"]'));
      // The Gmail help stays provider-specific under the field, its link in the console's link look.
      check((await page.textContent("[data-mbs-help]")).startsWith("Gmail refuses account passwords."), "form: Gmail help kept after the refusal");
      const link = page.locator("[data-mbs-help] a");
      check((await link.getAttribute("href")) === "https://myaccount.google.com/apppasswords" && (await link.getAttribute("target")) === "_blank", "form: the direct app-password link in a new tab");
      const linkInk = await link.evaluate((el) => getComputedStyle(el).color);
      const textInk = await page.$eval("#mailbox-sheet-title", (el) => getComputedStyle(el).color);
      check(linkInk === textInk && linkInk !== "rgb(0, 0, 238)", "form: link in the console's text colour, never the default blue", { linkInk, textInk });
      const sheetW = await page.$eval("#mailbox-sheet-backdrop .af-modal", (el) => Math.round(el.getBoundingClientRect().width));
      check(sheetW <= 480 && sheetW >= 440, "form: a narrow dialog (480 px) for a one-column form", sheetW);
      check(await page.inputValue("#mbs-password") === "account-password" && await page.inputValue("#mbs-address") === "someone@gmail.com", "form: the text is kept");
      const red = await page.$eval('#mailbox-sheet-body .mbs-error[role="alert"]', (el) => getComputedStyle(el).color);
      check(red !== await page.$eval("#mbs-address", (el) => getComputedStyle(el).color), "form: the error is coloured", red);
      await shotEl(page, "#mailbox-sheet-backdrop .af-modal", "mailbox-form-error-1440-light");
      // Microsoft without a sign-in client here: said plainly, no password, no action.
      await page.fill("#mbs-address", "me@outlook.com");
      await page.waitForSelector("[data-mbs-oauth-missing]");
      check((await page.textContent("[data-mbs-oauth-missing]")).trim() === "Microsoft mailboxes need Sign in with Microsoft; ask your gateway admin to register a Microsoft sign-in client.", "form: Microsoft needs its sign-in");
      check(await page.locator("#mbs-password").count() === 0 && await page.locator("#mailbox-sheet-go").isHidden(), "form: Microsoft — no password, no action");
      // Unknown provider: Server settings open, to fill in.
      await page.fill("#mbs-address", "me@unknown.example");
      await page.waitForSelector("details.mbs-servers[open] #mbs-imap-host");
      check((await page.textContent("[data-mbs-found]")).trim() === "We could not find this provider's settings; enter them below.", "form: unknown provider line");
      check(await page.inputValue("#mbs-login") === "me@unknown.example", "form: Sign-in name prefilled with the address");
      await shotEl(page, "#mailbox-sheet-backdrop .af-modal", "mailbox-form-unknown-1440-light");
      // Fastmail: an app password; Read and write off; Server settings collapsed and prefilled.
      await page.fill("#mbs-address", "me@fastmail.com");
      await page.waitForFunction(() => (document.querySelector("[data-mbs-found]") || {}).textContent === "Fastmail · use an app password");
      check(await page.inputValue("#mbs-password") === "", "form: another address starts the password afresh");
      check(await page.getAttribute("#mbs-write", "aria-checked") === "false" && (await page.textContent("#mbs-write")).includes("Read and write"), "form: Read and write, off by default");
      check((await page.textContent("#mailbox-sheet-body")).includes("Off: automations read this mailbox and never change it or send from it. On: they can mark, move, label, delete and send as it."), "form: Read and write says what each position means");
      check(await page.locator("#mbs-smtp-host").count() === 0, "form: no outgoing server while Read only");
      await page.click("#mbs-write");
      await page.waitForSelector("#mbs-smtp-host", { state: "attached" });
      check(await page.inputValue("#mbs-smtp-host") === "smtp.fastmail.com", "form: Read and write shows the outgoing server, prefilled");
      await page.click("#mbs-write");
      await page.waitForFunction(() => !document.getElementById("mbs-smtp-host"));
      check(await page.locator("details.mbs-servers:not([open])").count() === 1 && await page.inputValue("#mbs-imap-host") === "imap.fastmail.com", "form: Server settings collapsed, prefilled");
      const order = await page.$$eval("#mailbox-sheet-body label, #mailbox-sheet-body summary, #mailbox-sheet-body [role=switch]", (els) => els.map((e) => (e.getAttribute("role") === "switch" ? e.querySelector(".af-switch__label").textContent : e.textContent).trim()));
      check(JSON.stringify(order) === JSON.stringify(["Email address", "Password", "Read and write", "Server settings", "Incoming server (IMAP)", "Port", "Security", "Sign-in name"]), "form: field order", order);
      await page.fill("#mbs-password", "app-password");
      await shotEl(page, "#mailbox-sheet-backdrop .af-modal", "mailbox-form-fastmail-1440-light");
      await page.click("#mailbox-sheet-go");
      await page.waitForSelector('[data-mbx-row="me-fastmail-com"]');
      check(await page.locator("#mailbox-sheet-backdrop").isHidden(), "form: closes after adding");
      check(JSON.stringify(store.lastMailboxPut) === JSON.stringify({ id: "me-fastmail-com", body: { address: "me@fastmail.com", password: "app-password", test: true, imap: { host: "imap.fastmail.com", security: "ssl", port: 993 }, write: false, name: "me@fastmail.com" } }), "form: the PUT body (no name asked: the address)", store.lastMailboxPut);
      const row = (await page.textContent('[data-mbx-row="me-fastmail-com"] .mbx-row__line')).replace(/\s+/g, " ").trim();
      check(row === "me@fastmail.com · connected · Read only", "mailboxes: the added row, one line", row);
      // The row's Read and write switch: off (Read only), turning it on PUTs …/access {write: true}.
      const acc = page.locator("#my-mailbox-write-me-fastmail-com");
      check((await acc.getAttribute("role")) === "switch" && (await acc.textContent()).includes("Read and write") && (await acc.getAttribute("aria-checked")) === "false", "row: Read and write switch, off");
      await acc.click();
      await page.waitForFunction(() => (document.querySelector('[data-mbx-row="me-fastmail-com"] .mbx-row__line') || {}).textContent.includes("Read and write"));
      check(JSON.stringify(store.lastAccessPut) === JSON.stringify({ id: "me-fastmail-com", write: true }), "row: Read and write PUTs access {write: true}", store.lastAccessPut);
      check(await page.getAttribute("#my-mailbox-write-me-fastmail-com", "aria-checked") === "true", "row: the switch shows the new state");
      // The row: Active switch, "…" menu Test / Settings / Remove.
      const sw = page.locator("#my-mailbox-active-me-fastmail-com");
      check((await sw.getAttribute("role")) === "switch" && (await sw.textContent()).includes("Active"), "row: Active switch");
      await sw.click();
      await page.waitForFunction(() => { const b = document.getElementById("my-mailbox-active-me-fastmail-com"); return b && b.getAttribute("aria-checked") === "false"; });
      check(store.requests.includes("PUT /api/gateway/me/email/accounts/me-fastmail-com/enabled"), "row: Active PUTs enabled");
      await page.click('[data-mbx-row="me-fastmail-com"] .mbx-menu > summary');
      const menu = await page.$$eval('[data-mbx-row="me-fastmail-com"] .mbx-menu__list button', (els) => els.map((e) => e.textContent.trim()));
      check(JSON.stringify(menu) === JSON.stringify(["Test", "Settings", "Remove"]), "row: menu", menu);
      await page.click('[data-mbx-row="me-fastmail-com"] [data-mbx-test]');
      await page.waitForSelector('[data-mbx-row="me-fastmail-com"] .mbx-row__note.is-ok');
      await shotEl(page, "#account-email-backdrop .af-modal", "my-email-mailboxes-two-1440-light");
      await page.click('[data-mbx-row="me-fastmail-com"] .mbx-menu > summary');
      await page.click('[data-mbx-row="me-fastmail-com"] [data-mbx-delete]');
      check((await page.textContent('[data-mbx-row="me-fastmail-com"]')).includes("Remove me@fastmail.com?"), "row: Remove asks first");
      await page.click('[data-mbx-delete-now="me-fastmail-com"]');
      await page.waitForFunction(() => !document.querySelector('[data-mbx-row="me-fastmail-com"]'));
      check(store.requests.includes("DELETE /api/gateway/me/email/accounts/me-fastmail-com"), "row: Remove DELETEs");
    }
    if (w === 390) {
      // Phone width: the tabs and the mailbox form in one column, nothing wider than the screen.
      await page.waitForSelector('[data-trg-mailbox="default"]');
      await page.click('[data-trg-mailbox-manage="default"]');
      await page.waitForSelector('#my-mailboxes-list [data-mbx-row="__primary"]', { timeout: 30000 });
      await noSideScroll(page, "my email 390");
      await shotEl(page, "#account-email-backdrop .af-modal", "my-email-tab-mailboxes-390-light");
      await page.click("#my-mailbox-add");
      await page.fill("#mbs-address", "me@unknown.example");
      await page.waitForSelector("details.mbs-servers[open] #mbs-imap-host");
      await noSideScroll(page, "mailbox form 390");
      const widths = await page.$$eval("#mailbox-sheet-body input", (els) => els.map((e) => Math.round(e.getBoundingClientRect().right)));
      check(widths.every((r) => r <= 390), "mailbox form 390: every field inside the screen", widths);
      await shotEl(page, "#mailbox-sheet-backdrop .af-modal", "mailbox-form-unknown-390-light");
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
