import test from 'node:test';
import assert from 'node:assert/strict';
import { browser, profile, mockLedgerRead } from './support.mjs';

test('Portal adapter: explicit identity, fixed agent, fresh identity clears Run, no legacy writes', async () => {
  const engine = await browser();
  const context = await engine.newContext();
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  const requests = [];
  await page.route('**/v1/**', async route => {
    const req = route.request();
    requests.push({ method: req.method(), path: new URL(req.url()).pathname, token: req.headers().authorization });
    if (await mockLedgerRead(route)) return;
    return route.fulfill({ status: 404, json: { detail: 'not visible' } });
  });
  try {
    await page.goto(`${profile.url}/tests/browser/portal-workbench.html`);
    const apply = page.getByRole('button', { name: '应用门户登录身份并核对账本', exact: true });
    await apply.waitFor();
    assert.equal(requests.length, 0);
    const originalUrl = page.url();
    await page.getByRole('button', {name: '结果与待办', exact: true}).click();
    assert.equal(page.url(), originalUrl);
    assert.equal(await page.evaluate(() => document.activeElement?.id), 'workbench-results');
    assert.equal(requests.length, 0);
    await page.getByText('尚未读取 Run。', {exact: false}).waitFor();
    await page.getByText('尚未应用身份。', {exact: false}).waitFor();
    assert.equal(await page.getByLabel('用户访问令牌', { exact: true }).count(), 0);
    assert.equal(await page.getByLabel('Agent ID', { exact: true }).inputValue(), 'synthetic-agent');
    assert.equal(await page.getByLabel('Agent ID', { exact: true }).getAttribute('readonly'), '');
    await apply.click();
    await page.getByRole('status').filter({ hasText: '验签身份与账本读取完成' }).waitFor();
    assert.equal(requests[0].token, 'Bearer synthetic-1');
    await page.getByLabel('Run ID', { exact: true }).fill('old-run');
    await page.getByLabel('消息', { exact: true }).fill('old-message');
    await apply.click();
    await page.getByRole('status').filter({ hasText: '验签身份与账本读取完成' }).waitFor();
    assert.equal(await page.getByLabel('Run ID', { exact: true }).inputValue(), '');
    assert.equal(await page.getByLabel('消息', { exact: true }).inputValue(), '');
    assert.equal(requests.at(-1).token, 'Bearer synthetic-2');
    assert.equal(requests.every(req => req.method === 'GET'), true);
    assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
    await page.locator('#unmount').click();
    assert.equal(await apply.count(), 0);
  } finally { await context.close(); await engine.close(); }
});

test('Portal history: explicit subject filter, next page replacement, errors clear records', async () => {
  console.log("B1_HISTORY phase=launch");
  const engine = await browser();
  console.log("B1_HISTORY phase=context");
  const context = await engine.newContext();
  console.log("B1_HISTORY phase=page");
  const page = await context.newPage(); page.setDefaultTimeout(5000);
  let mode = 'first';
  await page.route('**/v1/**', async route => {
    if (await mockLedgerRead(route)) return;
    const url = new URL(route.request().url());
    assert.equal(route.request().method(), 'GET');
    assert.equal(url.pathname, '/v1/runs');
    assert.equal(url.searchParams.get('agent_id'), 'synthetic-agent');
    assert.equal(url.searchParams.get('subject'), 'human:alice');
    if (mode === 'error') return route.fulfill({ status: 403, body: 'PRIVATE' });
    const id = url.searchParams.has('cursor') ? 'second-run' : 'first-run';
    if (id === 'second-run') assert.equal(url.searchParams.get('cursor'), 'opaque');
    return route.fulfill({ json: { items: [{ run_id: id, subject: 'human:alice', agent_id: 'synthetic-agent', state: 'completed', last_seq: 3 }], next_cursor: id === 'first-run' ? 'opaque' : null } });
  });
  try {
    console.log("B1_HISTORY phase=navigate");
    await page.goto(`${profile.url}/tests/browser/portal-workbench.html`);
    await page.getByRole('button', { name: '应用门户登录身份并核对账本', exact: true }).click();
    await page.getByRole('status').filter({ hasText: '验签身份与账本读取完成' }).waitFor();
    await page.getByRole('button', { name: '读取本人 Run 首页', exact: true }).click();
    await page.getByText('first-run · completed', { exact: false }).waitFor();
    await page.getByRole('button', { name: '读取本人 Run 下一页', exact: true }).click();
    await page.getByText('second-run · completed', { exact: false }).waitFor();
    assert.equal(await page.getByText('first-run · completed', { exact: false }).count(), 0);
    assert.equal(await page.getByRole('button', { name: '读取本人 Run 下一页', exact: true }).isDisabled(), true);
    mode = 'error';
    await page.getByRole('button', { name: '读取本人 Run 首页', exact: true }).click();
    await page.getByRole('status').filter({ hasText: 'HTTP 403' }).waitFor();
    assert.equal(await page.getByRole('region', { name: '本人 Run 记录' }).getByRole('listitem').count(), 0);
    assert.equal((await page.locator('body').innerText()).includes('PRIVATE'), false);
  } finally {
    console.log("B1_HISTORY phase=context-close");
    await context.close();
    console.log("B1_HISTORY phase=browser-close");
    await engine.close();
    console.log("B1_HISTORY phase=closed");
  }
});


test('Workbench narrow viewport and keyboard navigation preserve route and never write', async () => {
  console.log("B1_NARROW phase=launch");
  const engine = await browser();
  console.log("B1_NARROW phase=new-page");
  const page = await engine.newPage({ viewport: { width: 390, height: 844 } });
  page.setDefaultTimeout(5000);
  const calls = [];
  await page.route('**/v1/**', route => { calls.push(route.request().method()); return route.fulfill({status: 503, json: {detail: 'synthetic unavailable'}}); });
  try {
    console.log("B1_NARROW phase=navigate");
    await page.goto(`${profile.url}/tests/browser/portal-workbench.html`);
    console.log("B1_NARROW phase=keyboard");
    const target = page.getByRole('button', {name: '结果与待办', exact: true});
    await target.focus(); await page.keyboard.press('Enter');
    assert.equal(await page.evaluate(() => document.activeElement?.id), 'workbench-results');
    const before = page.url();
    const identity = page.getByRole('button', {name: '身份与状态', exact: true});
    await identity.focus(); await page.keyboard.press('Space');
    assert.equal(await page.evaluate(() => document.activeElement?.id), 'workbench-identity');
    assert.equal(page.url(), before);
    assert.deepEqual(calls, []);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    const summary = page.locator('summary'); await summary.focus(); await page.keyboard.press('Enter');
    assert.equal(await page.locator('details').evaluate(el => el.open), true);
    await page.keyboard.press('Tab');
    assert.notEqual(await page.evaluate(() => document.activeElement?.tagName), 'BODY');
  } finally {
    console.log("B1_NARROW phase=page-close");
    await page.close();
    console.log("B1_NARROW phase=browser-close");
    await engine.close();
    console.log("B1_NARROW phase=closed");
  }
});
