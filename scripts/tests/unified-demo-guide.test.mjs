import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const {chromium} = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
test('统一体验入口：合成配置、Portal任务定位及未知结果指引', async () => {
 const html = await readFile(new URL('../local_demo.html', import.meta.url), 'utf8');
 assert.ok(!html.includes('\u0018DRM'), 'protected byte view: stop');
 const browser = await chromium.launch({channel:'chrome',headless:true});
 try {
  const page = await browser.newPage(); const unexpected=[];
  await page.route('**/*', async route => {
   const u = new URL(route.request().url());
   if (u.origin !== 'http://127.0.0.1:19876') { unexpected.push(u.origin); return route.abort(); }
   if (u.pathname === '/src/platform.css') return route.fulfill({contentType:'text/css',body:await readFile(new URL('../../apps/console-web/src/platform.css',import.meta.url),'utf8')});
   if (u.pathname === '/demo') return route.fulfill({contentType:'text/html',body:html});
   if (u.pathname === '/__demo/info') return route.fulfill({json:{portal:'http://127.0.0.1:19877/demo.html#/',tokens:{alice:'synthetic-alice',bob:'synthetic-bob'},expires_at:'2099-01-01T00:00:00Z',verified:null}});
   unexpected.push(u.pathname); return route.abort();
  });
  await page.goto('http://127.0.0.1:19876/demo');
  await page.waitForFunction(() => document.querySelector('#portal-tasks-link').href.endsWith('#/tasks'));
  assert.equal(await page.locator('#portal-tasks-link').getAttribute('href'),'http://127.0.0.1:19877/demo.html#/tasks');
  const region=page.getByRole('region',{name:'平台统一体验导航'});
  for (const path of ['/manage/agents','/chat','/manage/runs','/manage/operations','/manage/alerts','/manage/data-views','/a2a-host','/mcp-host','/manage/nodes','/manage/node-control']) {
   assert.equal(await region.locator(`a[href="${path}"]`).count(),1);
  }
  assert.match(await page.getByRole('complementary',{name:'任务异常处理'}).innerText(),/先只读核对原登记和回执，不再次执行/);
  assert.match(await region.innerText(),/普通演示不执行真实节点操作/);
  await page.setViewportSize({width:375,height:812});
  await page.locator('#portal-tasks-link').focus();
  assert.equal(await page.locator('#portal-tasks-link').evaluate(e=>e===document.activeElement),true);
  assert.deepEqual(unexpected,[]);
 } finally {await browser.close();}
});

for (const failure of ['http', 'disconnect', 'invalid-json']) {
 test(`演示信息失败明确提示且不自动重试：${failure}`, async () => {
  const html=await readFile(new URL('../local_demo.html',import.meta.url),'utf8');
  const browser=await chromium.launch({channel:'chrome',headless:true});
  try {
   const page=await browser.newPage(); let requests=0; const errors=[];
   page.on('pageerror',e=>errors.push(e.message));
   await page.route('**/*', async route=>{
    const u=new URL(route.request().url());
    if(u.origin!=='http://127.0.0.1:19876') return route.abort();
    if(u.pathname==='/demo') return route.fulfill({contentType:'text/html',body:html});
    if(u.pathname==='/src/platform.css') return route.fulfill({contentType:'text/css',body:await readFile(new URL('../../apps/console-web/src/platform.css',import.meta.url),'utf8')});
    if(u.pathname==='/__demo/info') {
     requests++;
     if(failure==='disconnect') return route.abort();
     return route.fulfill({status:failure==='http'?503:200,contentType:'application/json',body:'invalid'});
    }
    return route.abort();
   });
   await page.goto('http://127.0.0.1:19876/demo');
   await page.getByRole('alert').waitFor();
   assert.match(await page.getByRole('alert').innerText(),/不会自动重试或恢复旧写操作/);
   assert.equal(await page.locator('[data-copy]:disabled').count(),2);
   assert.equal(await page.locator('#portal-link').getAttribute('href'),null);
   assert.equal(requests,1); assert.deepEqual(errors,[]);
  } finally {await browser.close();}
 });
}
