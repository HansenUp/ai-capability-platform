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


test('Portal tasks: subject-bound pagination, filter cancellation, identity changes and errors clear rows', async () => {
  const engine = await browser(); const context = await engine.newContext(); const page = await context.newPage();
  page.setDefaultTimeout(5000);
  const calls = []; let mode = 'normal'; let release;
  const held = new Promise(resolve => { release = resolve; });
  await page.route('**/synthetic-tokens', route => route.fulfill({ json: { alice: 'alice', bob: 'bob' } }));
  await page.route('**/v1/**', async route => {
    const req = route.request(), url = new URL(req.url()); calls.push(req.method());
    const actor = req.headers().authorization === 'Bearer bob' ? 'bob' : 'alice';
    if (url.pathname === '/v1/workbench/identity') return route.fulfill({json:{ protocol:'workbench-v1', auth_method:'user', subject:`human:${actor}`, tenant_id:'default', scope_id:(actor === 'alice' ? 'a' : 'b').repeat(64) }});
    if (url.pathname === '/v1/workbench/attempts') return route.fulfill({json:{scope_id:(actor === 'alice' ? 'a' : 'b').repeat(64), active:null}});
    if (url.pathname === '/v1/agents') return route.fulfill({json:[]});
    assert.equal(url.pathname, '/v1/runs'); assert.equal(url.searchParams.get('subject'), `human:${actor}`);
    if (mode === 'error') return route.fulfill({status:403, body:'PRIVATE'});
    if (mode === 'held') await held;
    const id = url.searchParams.has('cursor') ? 'second' : 'first';
    if (id === 'second') assert.equal(url.searchParams.get('cursor'), 'opaque');
    await route.fulfill({json:{items:[{run_id:id, agent_id:url.searchParams.get('agent_id') || 'demo', subject:`human:${actor}`, state:'completed', last_seq:3}], next_cursor:id === 'first' ? 'opaque' : null}}).catch(() => {});
  });
  try {
    await page.goto(`${profile.url}/tests/browser/portal-flow.html`);
    await page.getByRole('link', { name:'我的任务', exact:true }).click();
    const first = page.getByRole('button', {name:'应用身份并读取任务首页',exact:true});
    await first.click(); await page.getByRole('link', {name:'查看任务 first',exact:true}).waitFor();
    await page.getByRole('button', {name:'读取任务下一页',exact:true}).click();
    await page.getByRole('link', {name:'查看任务 second',exact:true}).waitFor();
    assert.equal(await page.getByRole('link', {name:'查看任务 first',exact:true}).count(),0);
    assert.equal(await page.getByRole('button', {name:'读取任务下一页',exact:true}).isDisabled(),true);
    await first.click(); await page.getByRole('link', {name:'查看任务 first',exact:true}).waitFor();
    await page.getByLabel('合成身份',{exact:true}).selectOption('bob');
    await page.getByRole('button', {name:'读取任务下一页',exact:true}).click();
    await page.getByRole('status').filter({hasText:'身份已变化'}).waitFor();
    assert.equal(await page.getByRole('list',{name:'本人任务列表'}).locator('li').count(),0);
    mode='held'; const requested = page.waitForRequest(req => new URL(req.url()).pathname === '/v1/runs'); await first.click();
    await requested;
    await page.getByLabel('任务 Agent 筛选',{exact:true}).fill('new-agent'); release();
    assert.equal(await page.getByRole('list',{name:'本人任务列表'}).locator('li').count(),0);
    mode='error'; await first.click(); await page.getByRole('status').filter({hasText:'HTTP 403'}).waitFor();
    assert.equal(await page.getByRole('list',{name:'本人任务列表'}).locator('li').count(),0);
    assert.equal((await page.locator('body').innerText()).includes('PRIVATE'),false);
    assert.equal(calls.every(method => method === 'GET'),true);
  } finally { release(); await context.close(); await engine.close(); }
});
