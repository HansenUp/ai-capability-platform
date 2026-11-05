import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
import {join} from 'node:path';
import {browser,profile,phase} from '../../apps/console-web/tests/browser/support.mjs';
const portal=process.env.S12_PORTAL_URL,evidence=process.env.S12_BROWSER_EVIDENCE;
const prefix='统计创建时间从“2026-10-01T00:00:00Z”到“2026-10-02T00:00:00Z”的';
const compare='对比基准创建时间从“2026-10-01T00:00:00Z”到“2026-10-02T00:00:00Z”与当前创建时间从“2026-10-02T00:00:00Z”到“2026-10-03T00:00:00Z”的全部订单数量';
const samples=[
 {q:prefix+'全部订单数量',tool:'analytics.order_count',panel:'订单数量结果',draft:'填入订单统计草稿（不创建）',recover:true},
 ...['已完成订单金额，按币种分组','已完成订单平均金额，按币种分组','订单状态占比','全部订单数量，按日分组，时区UTC+08:00','全部订单数量，按日和状态分组，时区UTC','订单按日状态占比，时区UTC','已完成订单金额，按日和币种分组，时区UTC+08:00','已完成订单平均金额，按日和币种分组，时区UTC'].map(t=>({q:prefix+t,tool:'analytics.order_groups',panel:'订单分组结果',draft:'填入订单分组草稿（不创建）'})),
 {q:'统计创建时间从“2026-10-02T00:00:00Z”到“2026-10-03T00:00:00Z”的已完成订单金额，按币种分组',tool:'analytics.order_groups',panel:'订单分组结果',draft:'填入订单分组草稿（不创建）',missing:true},
 {q:'统计创建时间从“2026-10-03T00:00:00Z”到“2026-10-04T00:00:00Z”的订单状态占比',tool:'analytics.order_groups',panel:'订单分组结果',draft:'填入订单分组草稿（不创建）',empty:true},
 {q:compare,tool:'analytics.order_compare',panel:'订单双窗对比结果',draft:'填入订单对比草稿（不创建）'},
];
async function ack(page){await page.getByRole('button',{name:'预览本人确认收口',exact:true}).click();await page.getByRole('button',{name:'确认收口此回执',exact:true}).click();await page.waitForFunction(()=>![...document.querySelectorAll('button')].find(b=>b.textContent==='登记新 Run（尚未创建）')?.disabled);}
async function execute(page,token,sample,denied=false){
 await page.getByRole('button',{name:sample.draft,exact:true}).click();
 const original=await page.getByLabel('消息',{exact:true}).inputValue();
 const pending=page.waitForResponse(r=>r.url().endsWith('/v1/workbench/create-attempts')&&r.request().method()==='POST');await page.getByRole('button',{name:'登记新 Run（尚未创建）',exact:true}).click();const prepared=await(await pending).json();assert.equal(prepared.status,'PREPARED');
 if(sample.recover){
  let writes=0;const track=r=>{if(r.method()==='POST'&&new URL(r.url()).pathname.startsWith('/v1/'))writes++;};page.on('request',track);
  await page.reload();await page.getByLabel('查询描述',{exact:true}).fill(sample.q);await page.getByRole('button',{name:'生成门户查询候选',exact:true}).click();await page.getByRole('button',{name:'确认范围并打开门户查询工作台',exact:true}).click();await page.getByRole('button',{name:'应用门户登录身份并核对账本',exact:true}).click();await page.getByRole('status').filter({hasText:/已恢复服务端活动账本/}).waitFor();
  assert.equal(writes,0);page.off('request',track);assert.equal(await page.getByRole('button',{name:'登记新 Run（尚未创建）',exact:true}).isDisabled(),true);await page.getByLabel('登记时的原始创建消息',{exact:true}).fill(original);
 }
 await page.getByRole('button',{name:'确认执行已登记创建一次',exact:true}).click();await page.getByRole('button',{name:'预览本人确认收口',exact:true}).waitFor();await page.getByLabel('Run ID',{exact:true}).fill(prepared.run_id);await phase(page,prepared.run_id,'completed',token);
 const events=await(await page.request.get(profile.runtime+'/v1/runs/'+prepared.run_id+'/events',{headers:{Authorization:'Bearer '+token}})).json();const calls=events.filter(e=>e.type==='tool.requested'),results=events.filter(e=>e.type==='tool.result');assert.equal(calls.length,1);assert.equal(calls[0].payload.tool,sample.tool);assert.equal(results.length,1);
 const panel=page.getByRole('region',{name:sample.panel,exact:true});assert.equal(results[0].payload.status,denied?'error':'ok');
 if(denied)assert.equal(await panel.count(),0);else{await panel.waitFor();const out=results[0].payload.output;assert.equal(out.complete,true);if(sample.tool==='analytics.order_count')assert.equal(out.order_count,6);if(sample.tool==='analytics.order_compare'){assert.equal(out.baseline_count,6);assert.equal(out.current_count,1);assert.equal(out.delta_count,-5);assert.equal(out.relative_numerator,-5);assert.equal(out.relative_denominator,6);}if(sample.missing){assert.ok(out.groups.some(g=>g.total_minor===null&&!g.amount_complete));assert.match(await panel.innerText(),/正式总额未知/);}if(sample.empty){assert.equal(out.total_order_count,0);assert.match(await panel.innerText(),/空窗口占比未知/);}assert.equal(JSON.stringify(out).includes('order_id'),false);}
 await ack(page);return prepared.run_id;
}
test('F10真实手动启动profile：入口、Portal十二种查询、Console对比、Bob拒绝与同Run恢复',async()=>{
 assert.ok(portal&&evidence);const engine=await browser(),context=await engine.newContext(),page=await context.newPage();page.setDefaultTimeout(5000);page.on('pageerror',e=>console.error('F10_PAGE_ERROR',e.message));const origins=new Set([new URL(profile.url).origin,new URL(portal).origin]);const outside=[];
 await context.route('**/*',r=>{if(origins.has(new URL(r.request().url()).origin))return r.continue();outside.push(r.request().url());return r.abort();});
 const report={profile:'f10-readonly-v1',queries:[],model:'mock',production:false};
 try{
  await page.goto(profile.url+'/demo');await page.getByRole('status').filter({hasText:'本轮F10环境已启用'}).waitFor();const info=await(await page.request.get(profile.url+'/__demo/info')).json();assert.equal(info.profile,report.profile);assert.deepEqual(Object.keys(info.tokens).sort(),['alice','bob']);assert.deepEqual(info.tools,['analytics.order_count','analytics.order_groups','analytics.order_compare']);assert.equal(Object.hasOwn(info,'policy_tokens'),false);
  assert.equal((await page.request.get(profile.url+'/__demo/info',{headers:{Origin:'https://outside.invalid'}})).status(),403);
  await page.getByRole('link',{name:'Portal 受限查询',exact:true}).click();await page.getByRole('heading',{name:'Portal 受限查询',exact:true}).waitFor();
  for(const sample of samples){
   await page.goto(portal+'#/queries');await page.getByLabel('查询描述',{exact:true}).fill(sample.q);await page.getByRole('button',{name:'生成门户查询候选',exact:true}).click();await page.getByRole('button',{name:'确认范围并打开门户查询工作台',exact:true}).click();await page.getByRole('button',{name:'应用门户登录身份并核对账本',exact:true}).click();await page.getByRole('status').filter({hasText:/验签身份与账本读取完成|已恢复服务端活动账本/}).waitFor();
   const id=await execute(page,info.tokens.alice,sample);report.queries.push({tool:sample.tool,run_id:id,question:sample.q});
  }
  await page.goto(profile.url+'/manage/order-compare');await page.getByLabel('演示测试主体',{exact:true}).selectOption('alice');await page.getByRole('button',{name:'预览订单双窗对比',exact:true}).click();await page.getByRole('button',{name:'确认范围并进入订单对比工作台',exact:true}).click();await page.getByRole('button',{name:'应用演示身份并定位同Run',exact:true}).click();await page.getByRole('status').filter({hasText:/验签身份与账本读取完成/}).waitFor();
  report.console_run=await execute(page,info.tokens.alice,samples.at(-1));
  // 真实门户身份切换，不使用令牌粘贴或测试页面拦截。
  await page.goto(portal+'#/queries');await page.getByLabel('Portal测试主体',{exact:true}).selectOption('bob');await page.getByRole('button',{name:'应用测试身份',exact:true}).click();await page.getByRole('heading',{name:'Portal 受限查询',exact:true}).waitFor();await page.getByRole('button',{name:'预览门户查询',exact:true}).click();await page.getByRole('button',{name:'确认范围并打开门户查询工作台',exact:true}).click();await page.getByRole('button',{name:'应用门户登录身份并核对账本',exact:true}).click();await page.getByRole('status').filter({hasText:/验签身份与账本读取完成/}).waitFor();await execute(page,info.tokens.bob,{...samples[0],recover:false},true);
  assert.equal((await page.request.get(profile.runtime+'/v1/runs/'+report.console_run+'/events',{headers:{Authorization:'Bearer '+info.tokens.bob}})).status(),403);assert.deepEqual(outside,[]);assert.deepEqual(await page.evaluate(()=>[localStorage.length,sessionStorage.length]),[0,0]);report.status='PASS';
  await mkdir(evidence,{recursive:true});await writeFile(join(evidence,'result.json'),JSON.stringify(report,null,2));
 }catch(e){console.error('F10_PAGE_FAILURE',page.url(),(await page.locator('body').innerText()).slice(0,2500));throw e;}finally{await context.close();await engine.close();}
});
