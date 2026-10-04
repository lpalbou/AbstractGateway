// The OpenAI API page in a real browser at three widths (admin), then a user's own view, then the
// Accounts row's OpenAI API switch. The key card is filled client-side (masked, eye, copy).
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
const idle = (page) => page.waitForFunction(() => !document.querySelector('#openai-root [aria-busy="true"]'));
try {
  for (const [width, height] of [[1440, 1000], [768, 1024], [390, 844]]) {
    const context = await browser.newContext({ viewport: { width, height } });
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
    // The key: masked, revealed by the eye, masked again; the example never shows it in clear.
    const keyText = () => root.locator('[data-oai-key-value]').textContent();
    assert.match(await keyText(), /^•+$/);
    assert(!(await root.locator('[data-oai-code]').textContent()).includes(admin));
    await root.locator('[data-oai-action="reveal"]').click();
    assert.equal(await keyText(), admin);
    await root.locator('[data-oai-action="reveal"]').click();
    assert.match(await keyText(), /^•+$/);
    // A request shows in the log.
    assert.equal((await page.request.get(`${base}/v1/models`, { headers: { Authorization: `Bearer ${admin}` } })).status(), 200);
    await page.waitForSelector('[data-oai-logs] tbody tr', { timeout: 15000 });
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
    await page.screenshot({ path: path.join(output, `openai-api-${width}.png`), fullPage: true });
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
  // A user's own view: on/off, base URL, their key, docs, their requests; no access settings.
  await page.waitForSelector('#openai-root [data-oai-user-status]');
  assert.equal(await page.locator('#openai-root [data-oai-enabled]').count(), 0);
  assert.equal(await page.locator('#openai-root [data-oai-card="access"]').count(), 0);
  assert.equal(await page.locator('#openai-root [data-oai-action="new-key"]').count(), 1);
  await page.locator('#openai-root [data-oai-action="reveal"]').click();
  assert.equal(await page.locator('#openai-root [data-oai-key-value]').textContent(), 'endpoint-browser-user-001');
  await page.close();
  // Accounts: the admin turns alice's OpenAI API off; her key is refused at /v1 with the standard 403.
  const adminPage = await browser.newPage();
  adminPage.on('pageerror', (error) => errors.push(error.message));
  await signIn(adminPage, 'admin', admin);
  await adminPage.evaluate(() => document.getElementById('tab-button-users').click());
  const button = adminPage.locator('tr[data-user="alice"] [data-action="openai_api"]');
  await button.waitFor();
  await button.click();
  const toggle = adminPage.locator('#account-openai-body [role="switch"]');
  await toggle.waitFor();
  assert.equal(await toggle.getAttribute('aria-checked'), 'true');
  await toggle.click();
  await adminPage.waitForFunction(() => document.querySelector('#account-openai-body [role="switch"]').getAttribute('aria-checked') === 'false'
    && document.querySelector('#account-openai-body [role="switch"]').getAttribute('aria-busy') !== 'true');
  const started = await adminPage.request.post(`${base}/api/gateway/admin/core-endpoint`, { headers: { Authorization: `Bearer ${admin}` }, data: { enabled: true } });
  assert.equal(started.status(), 200);
  const refused = await adminPage.request.get(`${base}/v1/models`, { headers: { Authorization: 'Bearer endpoint-browser-user-001' } });
  assert.equal(refused.status(), 403);
  assert.equal((await refused.json()).error.code, 'openai_api_off');
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ ok: true, screens: 3 }));
} finally { await browser.close(); }
