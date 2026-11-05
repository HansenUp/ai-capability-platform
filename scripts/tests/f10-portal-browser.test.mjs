import test from 'node:test';import assert from 'node:assert/strict';import {readFile} from 'node:fs/promises';
import {browser,profile,phase} from '../../apps/console-web/tests/browser/support.mjs';
const url=profile.url+'/tests/browser/queries.html#/queries';
const prefix='统计创建时间从“2026-10-01T00:00:00Z”到“2026-10-02T00:00:00Z”的';
async function candidate(page,question=prefix+'全部订单数量'){
 await page.goto(url);await page.getByLabel('查询描述',{exact:true}).fill(question);await page.getByRole('button',{name:'生成门户查询候选',exact:true}).click();await page.getByRole('region',{name:'门户查询候选',exact:true}).waitFor();await page.getByRole('button',{name:'确认范围并打开门户查询工作台',exact:true}).click();
}
async function identity(page,actor='alice'){
 await page.getByLabel('合成身份',{exact:true}).selectOption(actor);await page.getByRole('button',{name:'应用门户登录身份并核对账本',exact:true}).click();await page.getByRole('status').filter({hasText:/验签身份与账本读取完成|已恢复服务端活动账本/}).waitFor();
}
async function ack(page){await page.getByRole('button',{name:'预览本人确认收口',exact:true}).click();await page.getByRole('button',{name:'确认收口此回执',exact:true}).click();await page.waitForFunction(()=>![...document.querySelectorAll('button')].find(b=>b.textContent==='登记新 Run（尚未创建）')?.disabled);}
test('Portal previews are local and invalid edits clear candidate; actual navigation exposes entry',async()=>{
 const engine=await browser(),context=await engine.newContext(),page=await context.newPage();const calls=[];
 await page.route('**/synthetic-tokens',r=>r.fulfill({json:{}}));await page.route('**/v1/**',r=>{calls.push(r.request().url());return r.abort();});
 try{
  await page.goto(url);await page.getByRole('button',{name:'预览门户查询',exact:true}).click();await page.getByRole('region',{name:'门户查询候选',exact:true}).waitFor();await page.getByLabel('创建终点（不含，含时区）',{exact:true}).fill('2026-10-01T00:00:00Z');assert.equal(await page.getByRole('region',{name:'门户查询候选',exact:true}).count(),0);await page.getByRole('button',{name:'预览门户查询',exact:true}).click();await page.getByRole('alert').waitFor();
  for(const text of ['统计昨天的订单数量',prefix+'全部订单数量，跨租户',prefix+'销售额']){await page.getByLabel('查询描述',{exact:true}).fill(text);await page.getByRole('button',{name:'生成门户查询候选',exact:true}).click();await page.getByRole('alert').waitFor();assert.equal(await page.getByRole('region',{name:'门户查询候选',exact:true}).count(),0);}
  assert.deepEqual(calls,[]);assert.deepEqual(await page.evaluate(()=>[localStorage.length,sessionStorage.length]),[0,0]);await page.getByRole('link',{name:'返回可用 Agent',exact:true}).click();await page.getByRole('link',{name:'受限查询',exact:true}).click();await page.getByRole('heading',{name:'Portal 受限查询',exact:true}).waitFor();
 }finally{await context.close();await engine.close();}
});
test('Portal signed identity → fixed Agent → actual tool results → owner ACK; reload is read only',async()=>{
 const tokens=JSON.parse(await readFile(profile.tokens,'utf8'));const engine=await browser(),context=await engine.newContext(),page=await context.newPage();page.setDefaultTimeout(5000);await page.route('**/synthetic-tokens',r=>r.fulfill({json:tokens}));
 const samples=[
  {q:prefix+'全部订单数量',tool:'analytics.order_count',panel:'订单数量结果',draft:'填入订单统计草稿（不创建）',recover:true},
  ...['已完成订单金额，按币种分组','已完成订单平均金额，按币种分组','订单状态占比','全部订单数量，按日分组，时区UTC+08:00','全部订单数量，按日和状态分组，时区UTC','订单按日状态占比，时区UTC'].map(t=>({q:prefix+t,tool:'analytics.order_groups',panel:'订单分组结果',draft:'填入订单分组草稿（不创建）'})),
  {q:'统计创建时间从“2026-10-02T00:00:00Z”到“2026-10-03T00:00:00Z”的已完成订单金额，按币种分组',tool:'analytics.order_groups',panel:'订单分组结果',draft:'填入订单分组草稿（不创建）',missing:true},
  {q:prefix.replaceAll('2026-10-01','2026-10-03').replaceAll('2026-10-02','2026-10-04')+'订单状态占比',tool:'analytics.order_groups',panel:'订单分组结果',draft:'填入订单分组草稿（不创建）',empty:true},
  {q:'对比基准创建时间从“2026-10-01T00:00:00Z”到“2026-10-02T00:00:00Z”与当前创建时间从“2026-10-02T00:00:00Z”到“2026-10-03T00:00:00Z”的全部订单数量',tool:'analytics.order_compare',panel:'订单双窗对比结果',draft:'填入订单对比草稿（不创建）'},
 ];
 try{for(const sample of samples){
  await candidate(page,sample.q);await identity(page);await page.getByRole('button',{name:sample.draft,exact:true}).click();assert.equal(await page.getByLabel('Agent ID',{exact:true}).isEditable(),false);
  const message=await page.getByLabel('消息',{exact:true}).inputValue();const pending=page.waitForResponse(r=>r.url().endsWith('/v1/workbench/create-attempts')&&r.request().method()==='POST');await page.getByRole('button',{name:'登记新 Run（尚未创建）',exact:true}).click();const prepared=await(await pending).json();assert.equal(prepared.status,'PREPARED');
  if(sample.recover){let writes=0;const listener=r=>{if(r.method()==='POST'&&new URL(r.url()).pathname.startsWith('/v1/'))writes++;};page.on('request',listener);await page.reload();await candidate(page,sample.q);await identity(page,'alice_fresh');assert.equal(writes,0);page.off('request',listener);assert.equal(await page.getByRole('button',{name:'登记新 Run（尚未创建）',exact:true}).isDisabled(),true);await page.getByLabel('登记时的原始创建消息',{exact:true}).fill(message);}
  await page.getByRole('button',{name:'确认执行已登记创建一次',exact:true}).click();await page.getByRole('button',{name:'预览本人确认收口',exact:true}).waitFor();await page.getByLabel('Run ID',{exact:true}).fill(prepared.run_id);await phase(page,prepared.run_id,'completed',tokens.alice);
  const events=await(await page.request.get(profile.runtime+'/v1/runs/'+prepared.run_id+'/events',{headers:{Authorization:'Bearer '+tokens.alice}})).json();const requested=events.filter(e=>e.type==='tool.requested'),results=events.filter(e=>e.type==='tool.result');assert.equal(requested.length,1);assert.equal(requested[0].payload.tool,sample.tool);assert.equal(results.length,1);assert.equal(results[0].payload.status,'ok');assert.equal(results[0].payload.output.complete,true);
  const panel=page.getByRole('region',{name:sample.panel,exact:true});await panel.waitFor();const out=results[0].payload.output;if(sample.missing){assert.ok(out.groups.some(g=>!g.amount_complete&&g.total_minor===null));assert.match(await panel.innerText(),/正式总额未知/);}if(sample.empty){assert.equal(out.total_order_count,0);assert.match(await panel.innerText(),/空窗口占比未知/);}if(sample.tool==='analytics.order_compare'){assert.equal(out.delta_count,out.current_count-out.baseline_count);assert.equal(out.relative_numerator,out.delta_count);}if(sample.tool==='analytics.order_count')assert.equal(out.order_count,6);
  for(const actor of ['bob','foreign'])assert.equal((await page.request.get(profile.runtime+'/v1/runs/'+prepared.run_id+'/events',{headers:{Authorization:'Bearer '+tokens[actor]}})).status(),actor==='foreign'?404:403);
  await ack(page);
 }}finally{await context.close();await engine.close();}
});
test('Portal Bob receives no implicit read_data; switching identity clears old results',async()=>{
 const tokens=JSON.parse(await readFile(profile.tokens,'utf8'));const engine=await browser(),context=await engine.newContext(),page=await context.newPage();page.setDefaultTimeout(5000);await page.route('**/synthetic-tokens',r=>r.fulfill({json:tokens}));
 try{
  await candidate(page);await identity(page,'bob');await page.getByRole('button',{name:'填入订单统计草稿（不创建）',exact:true}).click();const pending=page.waitForResponse(r=>r.url().endsWith('/v1/workbench/create-attempts')&&r.request().method()==='POST');await page.getByRole('button',{name:'登记新 Run（尚未创建）',exact:true}).click();const p=await(await pending).json();await page.getByRole('button',{name:'确认执行已登记创建一次',exact:true}).click();await page.getByRole('button',{name:'预览本人确认收口',exact:true}).waitFor();await page.getByLabel('Run ID',{exact:true}).fill(p.run_id);await phase(page,p.run_id,'completed',tokens.bob);const events=await(await page.request.get(profile.runtime+'/v1/runs/'+p.run_id+'/events',{headers:{Authorization:'Bearer '+tokens.bob}})).json();assert.equal(events.some(e=>e.type==='tool.result'&&e.payload.status==='error'),true);assert.equal(await page.getByRole('region',{name:'订单数量结果',exact:true}).count(),0);await ack(page);await identity(page);assert.equal(await page.getByLabel('Run ID',{exact:true}).inputValue(),'');assert.equal(await page.getByRole('region',{name:'订单数量结果',exact:true}).count(),0);
 }finally{await context.close();await engine.close();}
});
test('Portal lost execute response keeps one write and recovers original receipt without resend',async()=>{
 const tokens=JSON.parse(await readFile(profile.tokens,'utf8'));const engine=await browser(),context=await engine.newContext(),page=await context.newPage();page.setDefaultTimeout(5000);await page.route('**/synthetic-tokens',r=>r.fulfill({json:tokens}));
 try{
  await candidate(page);await identity(page);await page.getByRole('button',{name:'填入订单统计草稿（不创建）',exact:true}).click();const pending=page.waitForResponse(r=>r.url().endsWith('/v1/workbench/create-attempts')&&r.request().method()==='POST');await page.getByRole('button',{name:'登记新 Run（尚未创建）',exact:true}).click();const p=await(await pending).json();const path=`/v1/workbench/attempts/${p.attempt_id}/execute`;let executed=0;
  await page.route('**'+path,async r=>{executed++;await r.fetch();await r.abort('failed');});await page.getByRole('button',{name:'确认执行已登记创建一次',exact:true}).click();await page.getByRole('status').filter({hasText:/未知|unknown|fetch/i}).waitFor();assert.equal(executed,1);assert.equal(await page.getByRole('button',{name:'登记新 Run（尚未创建）',exact:true}).isDisabled(),true);
  let writes=0;page.on('request',r=>{if(r.method()==='POST'&&new URL(r.url()).pathname.startsWith('/v1/'))writes++;});await page.reload();await candidate(page);await identity(page);await page.getByRole('button',{name:'预览本人确认收口',exact:true}).waitFor();await page.getByLabel('Run ID',{exact:true}).fill(p.run_id);await phase(page,p.run_id,'completed',tokens.alice);await page.getByRole('region',{name:'订单数量结果',exact:true}).waitFor();assert.equal(writes,0);assert.equal(executed,1);await ack(page);
 }finally{await context.close();await engine.close();}
});
