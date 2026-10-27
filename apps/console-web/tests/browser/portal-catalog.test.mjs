import test from 'node:test';
import assert from 'node:assert/strict';
import { browser, profile } from './support.mjs';

test('Portal catalog DOM: scoped search, encoded link, errors and delayed old identity isolation', async () => {
  const engine = await browser();
  const context = await engine.newContext();
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  let mode = 'initial', release;
  const oldRequest = new Promise(resolve => { release = resolve; });
  await page.route('**/v1/agents', async route => {
    assert.equal(route.request().method(), 'GET');
    if (mode === 'late') {
      mode = 'new';
      await oldRequest;
      await route.fulfill({ json: [{ agent_id: 'PRIVATE-OLD', version: '1', rollout: 'stable' }] }).catch(() => {});
    } else if (mode === 'error') await route.fulfill({ status: 403, body: 'PRIVATE-ERROR' });
    else await route.fulfill({ json: [{ agent_id: mode === 'new' ? 'new-agent' : 'sales/a', version: '1', rollout: 'stable' }] });
  });
  try {
    await page.goto(`${profile.url}/tests/browser/portal-catalog.html`);
    const link = page.getByRole('link', { name: 'sales/a', exact: true });
    await link.waitFor();
    assert.equal(await link.getAttribute('href'), '/chat/sales%2Fa');
    await page.getByLabel('搜索 Agent', { exact: true }).fill('absent');
    await page.getByText('没有匹配的可用 Agent。', { exact: true }).waitFor();
    mode = 'error';
    await page.locator('#remount').click();
    await page.getByText('加载 Agent 列表失败：HTTP 403', { exact: true }).waitFor();
    assert.equal((await page.locator('body').innerText()).includes('PRIVATE-ERROR'), false);
    assert.equal(await page.getByRole('link').count(), 0);
    mode = 'late';
    const request = page.waitForRequest(req => new URL(req.url()).pathname === '/v1/agents');
    await page.locator('#remount').click(); await request;
    await page.locator('#remount').click();
    await page.getByRole('link', { name: 'new-agent', exact: true }).waitFor();
    release();
    assert.equal(await page.getByRole('link', { name: 'PRIVATE-OLD', exact: true }).count(), 0);
    assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
  } finally { release(); await context.close(); await engine.close(); }
});
