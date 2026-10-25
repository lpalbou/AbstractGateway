// Connected mailbox configuration: own account, entity, and OAuth. No real mail traffic.
import { createRequire } from "node:module";
import path from "node:path";
import fs from "node:fs";
const [BASE, ADMIN, PW] = process.argv.slice(2);
const { chromium } = createRequire(path.join(PW, "/"))("playwright-core");
const failures = [];
let checks = 0;
function check(ok, what, detail) { checks++; if (!ok) failures.push(`${what}: ${JSON.stringify(detail)}`); }
const browser = await chromium.launch();
try {
  for (const target of ["admin", "castor", "oauth"]) {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await ctx.newPage();
    page.on("pageerror", (e) => failures.push(e.message));
    const base = `/api/gateway/${target === "castor" ? "accounts/castor" : "me"}/email`;
    let saved = null, updates = [], deletes = 0, cancelOAuth = 0, rejectUpdate = true;
    let releaseUpdate;
    let finishOAuth, releaseCancel;
    await page.route(`**${base}`, async (route) => {
      if (route.request().method() === "GET") {
        if (!saved) {
          const real = await route.fetch();
          saved = { ...await real.json(), configured: true, address: "mailbox@example.org", username: "custom-login", enabled: false,
            auth_kind: target === "oauth" ? "oauth2" : "password",
            imap: { host: "custom-in.example.org", port: 1993, security: "ssl", folder: "Archive" },
            smtp: { host: "custom-out.example.org", port: 1587, security: "starttls" },
            mailbox: { state: "paused", address: "mailbox@example.org", reason: "Paused." },
            oauth: target === "oauth" ? { provider: "microsoft", client_id: "gateway-client", client_source: "own", tenant: "example.org" } : null,
            oauth_providers: [{ id: "microsoft", available: true }, { id: "google", available: true }] };
        }
        return route.fulfill({ json: saved });
      }
      if (route.request().method() === "DELETE") { deletes++; return route.fulfill({ json: {} }); }
      const body = route.request().postDataJSON(); updates.push(body);
      await new Promise((resolve) => { releaseUpdate = resolve; });
      if (rejectUpdate) return route.fulfill({ status: 422, json: { detail: { message: "Sign-in refused by custom-in.example.org", reason_code: "email_auth_failed" } } });
      saved = { ...saved, ...body };
      return route.fulfill({ json: saved });
    });
    await page.route(`**${base}/oauth/clients`, (r) => r.fulfill({ json: { providers: { microsoft: { configured: true, client_id: "gateway-client" } } } }));
    await page.route(`**${base}/oauth/start`, (r) => r.fulfill({ json: { flow_id: "test-flow", flow: "device", verification_uri: "https://example.org", user_code: "ABCD" } }));
    await page.route(`**${base}/oauth/finish`, async (r) => { await new Promise((resolve) => { finishOAuth = resolve; }); await r.fulfill({ json: { address: saved.address } }); });
    await page.route(`**${base}/oauth/cancel`, async (r) => { cancelOAuth++; await new Promise((resolve) => { releaseCancel = resolve; }); return r.fulfill({ json: { cancelled: true } }); });
    await page.goto(`${BASE}/console`);
    await page.fill("#login-user", "admin"); await page.fill("#login-token", ADMIN); await page.click("#login-button");
    await page.waitForFunction(() => document.body.classList.contains("signed-in"));
    await page.keyboard.press("Escape");
    await page.evaluate(() => { document.body.classList.remove("nav-open"); document.getElementById("tab-button-users").click(); });
    await page.click(`tr[data-user='${target === "castor" ? "castor" : "admin"}'] button[data-action='email']`);
    // My email → Mailboxes → the primary's "…" → Settings: the full editor in its own dialog.
    const openSettings = async () => {
      await page.waitForSelector('[data-mbx-row="__primary"] .mbx-menu > summary');
      await page.click('[data-mbx-row="__primary"] .mbx-menu > summary');
      await page.click('[data-mbx-settings="__primary"]');
      await page.waitForSelector("#my-email-connected", { state: "visible" });
    };
    const closeAll = async () => { await page.click("#mailbox-settings-close"); await page.click("#account-email-close"); };
    await openSettings();
    const actions = await page.locator(".mailbox-actions button").allTextContents();
    check(JSON.stringify(actions) === JSON.stringify(["Test", "Configure", "Disconnect"]), `${target}: actions`, actions);
    await page.click("#my-email-configure");
    await page.waitForSelector("#my-email-configure-actions", { state: "visible" });
    if (target === "oauth") {
      check(await page.getAttribute("#my-email-tab-microsoft", "aria-selected") === "true", "OAuth selects stored provider");
      check(await page.inputValue("#my-email-oauth-client-id") === "", "gateway OAuth client is not converted to personal override");
      await page.click("#my-email-oauth-start");
      await page.waitForSelector("#my-email-oauth-cancel", { state: "visible" });
      await page.click("#my-email-configure-cancel");
      while (!releaseCancel) await new Promise((r) => setTimeout(r, 10));
      await closeAll();
      await page.click("tr[data-user='admin'] button[data-action='email']");
      await openSettings();
      await page.click("#my-email-configure");
      await page.waitForSelector("#my-email-configure-actions", { state: "visible" });
      await page.fill("#my-email-oauth-client-secret", "fresh-client-secret");
      const cancelled = page.waitForResponse((r) => r.url().endsWith("/oauth/cancel"));
      releaseCancel(); await cancelled;
      finishOAuth();
      await page.waitForTimeout(100);
      check(cancelOAuth === 1 && deletes === 0, "OAuth Cancel cancels sign-in without disconnect", { cancelOAuth, deletes });
      check(await page.locator("#my-email-connect").isVisible() && await page.inputValue("#my-email-oauth-client-secret") === "fresh-client-secret", "late OAuth cancellation and finish cannot overwrite the reopened editor");
      await ctx.close(); continue;
    }
    const fields = ["address", "username", "imap-host", "imap-port", "smtp-host", "smtp-port", "smtp-security", "imap-folder"];
    const values = await Promise.all(fields.map((id) => page.inputValue(`#my-email-${id}`)));
    check(JSON.stringify(values) === JSON.stringify(["mailbox@example.org", "custom-login", "custom-in.example.org", "1993", "custom-out.example.org", "1587", "starttls", "Archive"]), `${target}: prefilled stored settings`, values);
    check(await page.inputValue("#my-email-password") === "", `${target}: no stored password exposed`);
    await page.fill("#my-email-imap-host", "draft.example.org"); await page.fill("#my-email-password", "draft-secret");
    await page.click("#my-email-configure-cancel");
    check(await page.locator("#my-email-connect").isHidden() && updates.length === 0 && deletes === 0, `${target}: Cancel does not mutate mailbox`);
    await page.click("#my-email-configure");
    check(await page.inputValue("#my-email-imap-host") === "custom-in.example.org" && await page.inputValue("#my-email-password") === "", `${target}: Cancel discards drafts`);
    await page.fill("#my-email-password", "new-secret");
    await page.fill("#my-email-smtp-port", "2587");
    await page.click("#my-email-connect-go");
    await page.waitForFunction(() => document.getElementById("my-email-connect-go").getAttribute("aria-busy") === "true");
    check(await page.isDisabled("#my-email-configure-cancel"), `${target}: no Cancel while saving`);
    releaseUpdate();
    await page.waitForSelector("#my-email-connect-error", { state: "visible" });
    check(await page.locator("#my-email-connect").isVisible() && saved.smtp.port === 1587 && deletes === 0, `${target}: failed update keeps editor and old connection`);
    rejectUpdate = false;
    await page.click("#my-email-connect-go");
    await page.waitForFunction(() => document.getElementById("my-email-connect-go").getAttribute("aria-busy") === "true");
    releaseUpdate();
    await page.waitForSelector("#my-email-connect", { state: "hidden" });
    check(updates[1].imap.folder === "Archive" && updates[1].username === "custom-login" && updates[1].smtp.port === 2587 && deletes === 0, `${target}: saves complete configuration without disconnect`, updates[1]);
    check(await page.getAttribute("#my-email-enabled", "aria-checked") === "false", `${target}: remains paused`);
    if (process.env.ABSTRACTGATEWAY_TEST_SCREENSHOT && target === "admin") {
      await page.click("#my-email-configure");
      const configure = await page.locator("#my-email-configure").evaluate((el) => {
        const style = getComputedStyle(el), rect = el.getBoundingClientRect();
        return { html: el.outerHTML, display: style.display, visibility: style.visibility, opacity: style.opacity, color: style.color, background: style.backgroundColor, rect: rect.toJSON(), stack: document.elementsFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2).map((e) => e.tagName + "#" + e.id) };
      });
      fs.writeFileSync(process.env.ABSTRACTGATEWAY_TEST_SCREENSHOT + ".json", JSON.stringify(configure));
      await page.screenshot({ path: process.env.ABSTRACTGATEWAY_TEST_SCREENSHOT });
    }
    if (target === "admin") {
      if (await page.locator("#my-email-connect").isHidden()) await page.click("#my-email-configure");
      await page.fill("#my-email-password", "pending-secret");
      releaseUpdate = null;
      await page.click("#my-email-connect-go");
      while (!releaseUpdate) await new Promise((r) => setTimeout(r, 10));
      await closeAll();
      await page.click("tr[data-user='admin'] button[data-action='email']");
      await openSettings();
      await page.click("#my-email-configure");
      await page.fill("#my-email-password", "fresh-draft");
      await page.fill("#my-email-imap-host", "fresh-draft.example.org");
      const completed = page.waitForResponse((r) => r.request().method() === "PUT" && r.url().endsWith(base));
      releaseUpdate(); await completed;
      await page.waitForTimeout(100);
      check(await page.locator("#my-email-connect").isVisible() && await page.inputValue("#my-email-password") === "fresh-draft" && await page.inputValue("#my-email-imap-host") === "fresh-draft.example.org", "a late save cannot clobber the reopened editor");
    }
    await ctx.close();
  }
} catch (e) { failures.push(String(e)); }
finally { await browser.close(); }
console.log(JSON.stringify({ checks, failures }));
