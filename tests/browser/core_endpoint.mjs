// The OpenAI API page in a real browser at three widths (admin), then a user's own view, then the
// Accounts row's OpenAI API switch and keys. API keys are named keys (round 16): New key asks for a
// name, the key goes straight to the clipboard and stays shown (Copy -> "Copied", Hide) across
// re-renders, resizes and leaving the page; the examples carry it (<your key> before); each row
// has Reveal (eye) and Copy (owner only, audited); the admin setting "API keys can be revealed by
// their owner" off = show-once; Revoke asks [Revoke] [Cancel] and applies at once. Light and dark.
import { createRequire } from 'node:module';
import path from 'node:path';
import assert from 'node:assert/strict';
const [base, admin, modules, output] = process.argv.slice(2);
const { chromium } = createRequire(path.join(modules, '/'))('playwright-core');
const browser = await chromium.launch({ headless: true });
const errors = [];
async function signIn(page, user, token) {
  await page.goto(`${base}/console`);
  await page.fill('#login-user', user);
  await page.fill('#login-token', token);
  await page.click('#login-button');
  await page.waitForFunction(() => document.body.classList.contains('signed-in'));
}
// The console's own theme setting (light | dark), as the other browser tests set it.
async function themed(options, theme) {
  const context = await browser.newContext({ permissions: ['clipboard-read', 'clipboard-write'], ...options, colorScheme: theme });
  await context.addInitScript((t) => { try { localStorage.setItem('abstractgateway_ui_settings_v1', JSON.stringify({ theme: t })); } catch {} }, theme);
  return context;
}
const idle = (page) => page.waitForFunction(() => !document.querySelector('#openai-root [aria-busy="true"]'));
const clip = (page) => page.evaluate(() => navigator.clipboard.readText());
try {
  for (const [width, height, theme] of [[1440, 1000, 'light'], [768, 1024, 'dark'], [390, 844, 'light']]) {
    const context = await themed({ viewport: { width, height } }, theme);
    const page = await context.newPage();
    page.on('pageerror', (error) => errors.push(error.message));
    await signIn(page, 'admin', admin);
    await page.evaluate(() => document.getElementById('tab-button-openai').click());
    const root = page.locator('#openai-root');
    const sw = root.locator('[data-oai-enabled]');
    await sw.waitFor();
    if (!(await sw.isChecked())) await sw.check();
    await page.waitForFunction(() => { const i = document.querySelector('[data-oai-enabled]'); return i.checked && !i.disabled; });
    assert.match(await root.locator('[data-oai-base]').textContent(), /\/v1$/);
    assert.equal((await page.request.get(`${base}/v1/models`)).status(), 401);
    // Authentication: Open, then back to Protected; the warning names who can use it.
    await root.locator('[data-oai-access="open"]').click();
    await idle(page);
    await page.waitForSelector('[data-oai-warning="open"]');
    await page.waitForSelector('[data-oai-open-note]');
    assert.equal(await root.locator('[data-oai-open-account]').inputValue(), 'guest');
    await root.locator('[data-oai-access="token"]').click();
    await idle(page);
    await page.waitForFunction(() => !document.querySelector('[data-oai-warning="open"]'));
    // Who can connect: Anywhere is locked with its reason until Internet mode.
    assert.equal(await root.locator('[data-oai-reach="anywhere"]').isDisabled(), true);
    await root.locator('[data-oai-reach="network"]').click();
    await idle(page);
    await page.waitForFunction(() => document.querySelector('[data-oai-reach="network"]').getAttribute('aria-checked') === 'true');
    await root.locator('[data-oai-reach="machine"]').click();
    await idle(page);
    // Docs card: the supported surface and a snippet with the real base URL.
    assert.match(await root.locator('[data-oai-support]').textContent(), /Supported:.*Not yet:/s);
    await root.locator('[data-oai-snippet="python"]').click();
    assert.match(await root.locator('[data-oai-code]').textContent(), new RegExp(`base_url="${base}/v1"`));
    // API keys: never the gateway token; New key -> name -> shown once -> works at /v1 -> listed -> revoked.
    assert.equal(await root.locator('[data-oai-key-value], [data-oai-action="reveal"]').count(), 0);
    assert(!(await root.textContent()).includes(admin));
    assert.match(await root.locator('[data-oai-code]').textContent(), /<your key>/);
    assert.match(await root.locator('[data-oai-key]').textContent(), /One key can serve every app\. Separate keys are optional/);
    if (width === 1440) await page.waitForSelector('[data-oai-keys-empty]');
    await root.locator('[data-oai-action="new-key"]').click();
    await root.locator('[data-oai-key-label]').fill(`laptop ${width}`);
    await root.locator('[data-oai-key-make]').click();
    await page.waitForSelector('[data-oai-made-key]');
    const made = (await root.locator('[data-oai-made-key]').textContent()).trim();
    assert.match(made, /^sk-agw-/);
    // Straight to the clipboard, and the sentence says so; it can be revealed again.
    await page.waitForFunction(() => /made and copied to the clipboard/.test(document.querySelector('[data-oai-made]').textContent));
    assert.equal(await clip(page), made);
    assert.match(await root.locator('[data-oai-made-policy]').textContent(), /reveal it again any time/);
    // The examples carry the real key (curl and Python), and Copy example copies exactly that.
    assert.match(await root.locator('[data-oai-code]').textContent(), new RegExp(`api_key="${made}"`));
    await root.locator('[data-oai-snippet="curl"]').click();
    assert.match(await root.locator('[data-oai-code]').textContent(), new RegExp(`Bearer ${made}`));
    await page.evaluate(() => navigator.clipboard.writeText('-'));
    await root.locator('[data-oai-copy-snippet]').click();
    await page.waitForFunction(() => document.querySelector('[data-oai-copy-snippet]').textContent === 'Copied');
    assert.match(await clip(page), new RegExp(`Bearer ${made}`));
    // Copy beside the key: the kit tooltip, "Copied", the key on the clipboard.
    assert.equal(await root.locator('[data-oai-action="copy-made"]').getAttribute('data-af-tip'), 'Copy the API key');
    await page.evaluate(() => navigator.clipboard.writeText('-'));
    await root.locator('[data-oai-action="copy-made"]').click();
    await page.waitForFunction(() => document.querySelector('[data-oai-action="copy-made"]').textContent === 'Copied');
    assert.equal(await clip(page), made);
    // A re-render, a resize and leaving the page never lose it (operator feedback 2026-10-09).
    await page.setViewportSize({ width: Math.max(360, width - 300), height: height - 200 });
    await page.evaluate(() => document.getElementById('tab-button-network').click());
    await page.evaluate(() => document.getElementById('tab-button-openai').click());
    await page.setViewportSize({ width, height });
    await page.waitForSelector('[data-oai-made-key]');
    assert.equal((await root.locator('[data-oai-made-key]').textContent()).trim(), made);
    const row = root.locator('[data-oai-keyrow]', { hasText: `laptop ${width}` });
    await row.waitFor();
    assert.match(await row.textContent(), /Never used/);
    assert.equal((await page.request.get(`${base}/v1/models`, { headers: { Authorization: `Bearer ${made}` } })).status(), 200);
    // Endpoint-only: the same key is refused by the console API, with the sentence.
    const outside = await page.request.get(`${base}/api/gateway/openai-api`, { headers: { Authorization: `Bearer ${made}` } });
    assert.equal(outside.status(), 401);
    assert.equal((await outside.json()).detail, 'This is an API key for /v1; sign in with your gateway token.');
    await page.screenshot({ path: path.join(output, `openai-keys-made-${width}-${theme}.png`), fullPage: true });
    await root.locator('[data-oai-action="made-done"]').click();
    assert.equal(await root.locator('[data-oai-made-key]').count(), 0);
    assert.match(await root.locator('[data-oai-code]').textContent(), /<your key>/);
    // Reveal (eye, kit tooltip) shows it again and fills the examples; the row's Copy copies it.
    const eye = row.locator('[data-oai-reveal]');
    assert.match(await eye.getAttribute('data-af-tip'), /only you can, and each reveal is noted in the audit log/);
    assert.equal(await eye.locator('svg').count(), 1);
    await eye.click();
    await page.waitForSelector('[data-oai-made="revealed"] [data-oai-made-key]');
    assert.equal((await root.locator('[data-oai-made-key]').textContent()).trim(), made);
    assert.match(await root.locator('[data-oai-code]').textContent(), new RegExp(made));
    await root.locator('[data-oai-action="made-done"]').click();
    await page.evaluate(() => navigator.clipboard.writeText('-'));
    await row.locator('[data-oai-copy-key]').click();
    await page.waitForFunction((f) => document.querySelector(`[data-oai-copy-key="${f}"]`).textContent === 'Copied', await row.getAttribute('data-oai-keyrow'));
    assert.equal(await clip(page), made);
    if (width === 1440) {
      // The admin setting off: show-once; the row's Reveal and Copy say why, nothing is revealed.
      const setting = root.locator('[data-oai-owner-reveal]');
      assert.equal(await setting.isChecked(), true);
      await setting.uncheck();
      await page.waitForFunction(() => /stored keys were erased/.test((document.querySelector('[data-oai-notice]') || {}).textContent || ''));
      await page.waitForFunction(() => document.querySelector('[data-oai-reveal]').getAttribute('aria-disabled') === 'true');
      await row.locator('[data-oai-reveal]').click({ force: true });  // aria-disabled: a press says why
      await page.waitForFunction(() => /Revealing keys is turned off/.test((document.querySelector('[data-oai-key-notice]') || {}).textContent || ''));
      assert.equal(await root.locator('[data-oai-made-key]').count(), 0);
      await root.locator('[data-oai-owner-reveal]').check();
      await page.waitForFunction(() => /owners can reveal/.test((document.querySelector('[data-oai-notice]') || {}).textContent || ''));
      // ...and the key made before stays hash-only (its sealed copy was erased).
      await page.waitForFunction(() => document.querySelector('[data-oai-reveal]').getAttribute('aria-disabled') === 'true');
      assert.match(await row.locator('[data-oai-reveal]').getAttribute('data-af-tip'), /Made while revealing was off/);
    }
    // A request shows in the log, named by its key.
    assert.equal((await page.request.get(`${base}/v1/models`, { headers: { Authorization: `Bearer ${made}` } })).status(), 200);
    await page.waitForSelector('[data-oai-logs] tbody tr', { timeout: 15000 });
    await page.waitForFunction((label) => [...document.querySelectorAll('[data-oai-key-label-cell]')].some((c) => c.textContent === label), `laptop ${width}`, { timeout: 15000 });
    // A row opens to the recorded request and response (keys removed), with Open in Observer.
    const okRid = await (await page.waitForFunction(() => {
      const row = [...document.querySelectorAll('#openai-root tr.oai-row')]
        .find((tr) => (tr.querySelector('td[data-label="Status"]') || {}).textContent?.trim() === '200');
      return row ? row.getAttribute('data-oai-row') : null;
    }, null, { timeout: 15000 })).jsonValue();
    await root.locator(`[data-oai-toggle="${okRid}"]`).click();
    await page.waitForSelector('[data-oai-detail] [data-oai-json="response"]');
    assert.match(await root.locator('[data-oai-detail] [data-oai-json="response"]').textContent(), /"object": "list"/);
    assert.equal(await root.locator('[data-oai-detail] [data-oai-observer]').count(), 1);
    assert(!(await root.locator('[data-oai-detail]').textContent()).includes(admin));
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `overflow at ${width}`);
    for (const card of await root.locator('[data-oai-card]').all()) {
      const b = await card.boundingBox();
      assert(b.x >= 0 && b.x + b.width <= width + 1, `card bounds at ${width}`);
    }
    await page.screenshot({ path: path.join(output, `openai-api-${width}-${theme}.png`), fullPage: true });
    // Revoke: [Revoke] [Cancel]; Cancel keeps it, Revoke refuses it at once.
    const fp = await row.getAttribute('data-oai-keyrow');
    await row.locator('[data-oai-revoke]').click();
    await page.waitForSelector(`[data-oai-revoke-yes="${fp}"]`);
    await root.locator('[data-oai-action="revoke-no"]').click();
    assert.equal(await root.locator(`[data-oai-revoke-yes="${fp}"]`).count(), 0);
    assert.equal((await page.request.get(`${base}/v1/models`, { headers: { Authorization: `Bearer ${made}` } })).status(), 200);
    await row.locator('[data-oai-revoke]').click();
    await root.locator(`[data-oai-revoke-yes="${fp}"]`).click();
    await page.waitForFunction((f) => !document.querySelector(`[data-oai-keyrow="${f}"]`), fp);
    assert.match(await root.locator('[data-oai-key-notice]').textContent(), /Revoked/);
    const gone = await page.request.get(`${base}/v1/models`, { headers: { Authorization: `Bearer ${made}` } });
    assert.equal(gone.status(), 401);
    assert.equal((await gone.json()).error.code, 'invalid_api_key');
    await sw.uncheck();
    await page.waitForFunction(() => { const i = document.querySelector('[data-oai-enabled]'); return !i.checked && !i.disabled; });
    assert.equal((await page.request.get(`${base}/v1/models`, { headers: { Authorization: `Bearer ${admin}` } })).status(), 404);
    // The Network page keeps one pointer, no endpoint controls.
    await page.evaluate(() => document.getElementById('tab-button-network').click());
    await page.waitForSelector('[data-net-openai-pointer]');
    assert.equal(await page.locator('#network-root [data-oai-enabled], #network-root [data-core-enabled]').count(), 0);
    await context.close();
  }
  const page = await browser.newPage();
  page.on('pageerror', (error) => errors.push(error.message));
  await signIn(page, 'alice', 'endpoint-browser-user-001');
  await page.evaluate(() => document.getElementById('tab-button-openai').click());
  // A user's own view: on/off, base URL, their keys, docs, their requests; no access settings.
  await page.waitForSelector('#openai-root [data-oai-user-status]');
  assert.equal(await page.locator('#openai-root [data-oai-enabled]').count(), 0);
  assert.equal(await page.locator('#openai-root [data-oai-card="access"]').count(), 0);
  await page.waitForSelector('#openai-root [data-oai-keys-empty]');
  assert(!(await page.locator('#openai-root').textContent()).includes('endpoint-browser-user-001'));
  await page.locator('#openai-root [data-oai-action="new-key"]').click();
  await page.locator('#openai-root [data-oai-key-label]').fill('home assistant');
  await page.locator('#openai-root [data-oai-key-label]').press('Enter');
  await page.waitForSelector('#openai-root [data-oai-made-key]');
  const aliceKey = (await page.locator('#openai-root [data-oai-made-key]').textContent()).trim();
  await page.close();
  // Accounts: the admin turns alice's OpenAI API off; her key is refused at /v1 with the standard 403.
  async function accountModal(theme) {
    const p = await (await themed({ viewport: { width: 1280, height: 800 } }, theme)).newPage();
    p.on('pageerror', (error) => errors.push(error.message));
    await signIn(p, 'admin', admin);
    await p.evaluate(() => document.getElementById('tab-button-users').click());
    const b = p.locator('tr[data-user="alice"] [data-action="openai_api"]');
    await b.waitFor();
    await b.click();
    await p.locator('#account-openai-body [data-account-openai-key]', { hasText: 'home assistant' }).waitFor();
    await p.screenshot({ path: path.join(output, `account-openai-keys-${theme}.png`) });
    return p;
  }
  await (await accountModal('dark')).context().close();
  const adminPage = await accountModal('light');
  const toggle = adminPage.locator('#account-openai-body [role="switch"]');
  await toggle.waitFor();
  // The admin sees alice's keys (never a key) and can revoke them.
  const keyRow = adminPage.locator('#account-openai-body [data-account-openai-key]', { hasText: 'home assistant' });
  await keyRow.waitFor();
  assert(!(await adminPage.locator('#account-openai-body').textContent()).includes(aliceKey));

  assert.equal(await toggle.getAttribute('aria-checked'), 'true');
  await toggle.click();
  await adminPage.waitForFunction(() => document.querySelector('#account-openai-body [role="switch"]').getAttribute('aria-checked') === 'false'
    && document.querySelector('#account-openai-body [role="switch"]').getAttribute('aria-busy') !== 'true');
  const started = await adminPage.request.post(`${base}/api/gateway/admin/core-endpoint`, { headers: { Authorization: `Bearer ${admin}` }, data: { enabled: true } });
  assert.equal(started.status(), 200);
  const refused = await adminPage.request.get(`${base}/v1/models`, { headers: { Authorization: 'Bearer endpoint-browser-user-001' } });
  assert.equal(refused.status(), 403);
  assert.equal((await refused.json()).error.code, 'openai_api_off');
  // Every key of the account obeys the switch.
  const keyOff = await adminPage.request.get(`${base}/v1/models`, { headers: { Authorization: `Bearer ${aliceKey}` } });
  assert.equal(keyOff.status(), 403);
  assert.equal((await keyOff.json()).error.code, 'openai_api_off');
  // The admin revokes alice's key: [Revoke] [Cancel] inline, then 401 at once.
  await keyRow.locator('[data-account-openai-revoke]').click();
  await adminPage.locator('#account-openai-body .inline-confirm button.danger').click();
  await adminPage.waitForSelector('#account-openai-body [data-account-openai-keys-empty]');
  const revoked = await adminPage.request.get(`${base}/v1/models`, { headers: { Authorization: `Bearer ${aliceKey}` } });
  assert.equal(revoked.status(), 401);
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ ok: true, screens: 3, keys: true }));
} finally { await browser.close(); }
