import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {apply,browser,phase,profile} from './support.mjs';

test('状态占比预览：固定分母、拒绝单状态与币种、统一问法入口无业务请求',async()=>{
 const engine=await browser(),context=await engine.newContext(),page=await context.newPage();page.setDefaultTimeout(5000);const calls=[];
 for(const pattern of ['**/console/**','**/v1/**'])await page.route(pattern,async r=>{calls.push(r.request().url());await r.abort();});
 try{
  await page.goto(profile.url+'/manage/order-groups');await page.getByLabel('统计指标',{exact:true}).selectOption('status_share');
  const build=page.getByRole('button',{name:'预览订单分组查询',exact:true}),candidate=page.getByRole('region',{name:'订单分组候选',exact:true});
  await build.click();await page.getByRole('alert').waitFor();assert.equal(await candidate.count(),0);
  await page.getByLabel('分组当前状态',{exact:true}).selectOption('all');await page.getByLabel('分组维度',{exact:true}).selectOption('status');
  await build.click();await candidate.waitFor();const args=JSON.parse(await candidate.locator('pre').innerText());assert.equal(args.status,'all');assert.equal(args.metric,'status_share');assert.equal(args.group_by,'status');
  await page.getByLabel('分组当前状态',{exact:true}).selectOption('pending');assert.equal(await candidate.count(),0);await build.click();await page.getByRole('alert').waitFor();
  const text='统计创建时间从“2026-10-01T00:00:00Z”到“2026-10-02T00:00:00Z”的订单状态占比';
  await page.getByLabel('订单分组描述',{exact:true}).fill(text);await page.getByRole('button',{name:'生成订单分组候选',exact:true}).click();assert.equal(JSON.parse(await candidate.locator('pre').innerText()).status,'all');
  await page.getByRole('link',{name:'受限查询统一入口与能力说明',exact:true}).click();await page.getByLabel('统一查询描述',{exact:true}).fill(text);
  await page.getByRole('button',{name:'评估问法并生成候选',exact:true}).click();await page.getByRole('heading',{name:'已支持（待授权候选）',exact:true}).waitFor();
  assert.equal(JSON.parse(await page.getByLabel('统一请求候选',{exact:true}).innerText()).metric,'status_share');
  assert.deepEqual(calls,[]);assert.deepEqual(await page.evaluate(()=>[localStorage.length,sessionStorage.length]),[0,0]);
 }finally{await context.close();await engine.close();}
});

test('状态占比：正常/零类别/空窗口、权限拒绝、原人恢复与ACK',async()=>{
 const tokens=JSON.parse(await readFile(profile.tokens,'utf8'));
 const engine=await browser(),context=await engine.newContext(),page=await context.newPage();page.setDefaultTimeout(5000);
 try{
  for(const sample of [{day:1,total:3,counts:[1,2,0]},{day:2,total:1,counts:[0,1,0]},{day:3,total:0,counts:[0,0,0]},{day:6,total:1,counts:[0,1,0]},
   {day:1,denied:true,token:tokens.bob},{day:1,denied:true,token:tokens.feedback_admin},{day:1,denied:true,invalid:true}]){
   const token=sample.token??tokens.alice;
   await page.goto(profile.url+'/manage/order-groups');await page.getByLabel('分组创建起点（含时区）',{exact:true}).fill(`2026-10-${String(sample.day).padStart(2,'0')}T00:00:00Z`);
   await page.getByLabel('分组创建终点（不含，含时区）',{exact:true}).fill(`2026-10-${String(sample.day+1).padStart(2,'0')}T00:00:00Z`);
   await page.getByLabel('统计指标',{exact:true}).selectOption('status_share');await page.getByLabel('分组当前状态',{exact:true}).selectOption('all');await page.getByLabel('分组维度',{exact:true}).selectOption('status');
   await page.getByRole('button',{name:'预览订单分组查询',exact:true}).click();await page.getByRole('button',{name:'确认范围并进入订单分组工作台',exact:true}).click();
   await apply(page,token);await page.getByRole('button',{name:'只读核对写账本',exact:true}).click();await page.getByRole('button',{name:'填入订单分组草稿（不创建）',exact:true}).click();
   if(sample.invalid){const input=page.getByLabel('消息',{exact:true}),text=await input.inputValue();const args=JSON.parse(text.replace('!tool analytics.order_groups ',''));args.status='completed';await input.fill('!tool analytics.order_groups '+JSON.stringify(args));}
   const pending=page.waitForResponse(r=>r.url().endsWith('/v1/workbench/create-attempts')&&r.request().method()==='POST');await page.getByRole('button',{name:'登记新 Run（尚未创建）',exact:true}).click();
   const prepared=await(await pending).json();assert.equal(prepared.status,'PREPARED');await page.getByRole('button',{name:'确认执行已登记创建一次',exact:true}).click();await page.getByRole('button',{name:'预览本人确认收口',exact:true}).waitFor();
   await page.getByLabel('Run ID',{exact:true}).fill(prepared.run_id);await phase(page,prepared.run_id,'completed',token);
   const events=await(await page.request.get(`${profile.runtime}/v1/runs/${prepared.run_id}/events`,{headers:{Authorization:`Bearer ${token}`}})).json();const results=events.filter(e=>e.type==='tool.result');assert.equal(results.length,1);
   const panel=page.getByRole('region',{name:'订单分组结果',exact:true});
   if(sample.denied){assert.equal(results[0].payload.status,'error');assert.equal(await panel.count(),0);}
   else{
    assert.equal(results[0].payload.status,'ok');const out=results[0].payload.output;assert.equal(out.total_order_count,sample.total);assert.equal(out.groups.length,3);assert.deepEqual(out.groups.map(g=>g.order_count),sample.counts);
    assert.deepEqual(out.groups.map(g=>g.share_denominator),[sample.total||null,sample.total||null,sample.total||null]);assert.deepEqual(out.groups.map(g=>g.share_numerator),sample.total?sample.counts:[null,null,null]);
    await panel.waitFor();assert.equal(await panel.locator('tbody tr').count(),3);if(!sample.total)assert.match(await panel.innerText(),/空窗口占比未知/);else assert.match(await panel.innerText(),/0 \/ [13]/);
    assert.equal(JSON.stringify(out).includes('order_id'),false);assert.equal(JSON.stringify(out).includes('NEVER-MODEL'),false);
    for(const other of [tokens.bob,tokens.feedback_admin,tokens.foreign])assert.equal((await page.request.get(`${profile.runtime}/v1/runs/${prepared.run_id}/events`,{headers:{Authorization:`Bearer ${other}`}})).status(),other===tokens.foreign?404:403);
   }
   if(sample.total===3){
    let writes=0;const track=r=>{if(r.method()==='POST'&&new URL(r.url()).pathname.startsWith('/v1/'))writes++;};page.on('request',track);await page.reload();
    await page.getByLabel('统计指标',{exact:true}).selectOption('status_share');await page.getByLabel('分组当前状态',{exact:true}).selectOption('all');await page.getByLabel('分组维度',{exact:true}).selectOption('status');
    await page.getByRole('button',{name:'预览订单分组查询',exact:true}).click();await page.getByRole('button',{name:'确认范围并进入订单分组工作台',exact:true}).click();await apply(page,token);await page.getByRole('button',{name:'只读核对写账本',exact:true}).click();await page.getByRole('button',{name:'预览本人确认收口',exact:true}).waitFor();assert.equal(writes,0);page.off('request',track);
    assert.equal(await page.getByRole('button',{name:'登记新 Run（尚未创建）',exact:true}).isDisabled(),true);
   }
   await page.getByRole('button',{name:'预览本人确认收口',exact:true}).click();await page.getByRole('button',{name:'确认收口此回执',exact:true}).click();
   await page.waitForFunction(()=>{const b=[...document.querySelectorAll('button')].find(b=>b.textContent?.trim()==='登记新 Run（尚未创建）');return b&&!b.matches(':disabled');});
  }
 }finally{await context.close();await engine.close();}
});
