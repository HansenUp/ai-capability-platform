import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {apply,browser,phase,profile} from './support.mjs';
const question='统计创建时间从“2026-10-01T00:00:00Z”到“2026-10-02T00:00:00Z”的全部订单数量，按日分组，时区UTC+08:00';
async function preview(page,{metric='order_count',zone='UTC+08:00',since='2026-10-01T00:00:00Z',until='2026-10-02T00:00:00Z'}={}){
 await page.goto(profile.url+'/manage/order-groups');
 await page.getByLabel('分组创建起点（含时区）',{exact:true}).fill(since);await page.getByLabel('分组创建终点（不含，含时区）',{exact:true}).fill(until);
 await page.getByLabel('统计指标',{exact:true}).selectOption(metric);await page.getByLabel('分组当前状态',{exact:true}).selectOption(metric==='order_count'?'all':'completed');
 await page.getByLabel('分组维度',{exact:true}).selectOption(metric==='order_count'?'day':'day_currency');await page.getByLabel('分桶时区',{exact:true}).selectOption(zone);
 await page.getByRole('button',{name:'预览订单分组查询',exact:true}).click();
}
test('按日预览明确时区、编辑清空候选、自然问法统一入口不发业务请求',async()=>{
 const engine=await browser(),context=await engine.newContext(),page=await context.newPage();page.setDefaultTimeout(5000);const calls=[];
 for(const pattern of ['**/console/**','**/v1/**'])await page.route(pattern,async r=>{calls.push(r.request().url());await r.abort();});
 try{
  await preview(page,{zone:''});await page.getByRole('alert').waitFor();const candidate=page.getByRole('region',{name:'订单分组候选',exact:true});assert.equal(await candidate.count(),0);
  await page.getByLabel('分桶时区',{exact:true}).selectOption('UTC+08:00');await page.getByRole('button',{name:'预览订单分组查询',exact:true}).click();await candidate.waitFor();assert.equal(JSON.parse(await candidate.locator('pre').innerText()).bucket_timezone,'UTC+08:00');
  await page.getByLabel('分桶时区',{exact:true}).selectOption('UTC');assert.equal(await candidate.count(),0);
  await page.getByLabel('统计指标',{exact:true}).selectOption('completed_amount');await page.getByRole('button',{name:'预览订单分组查询',exact:true}).click();await page.getByRole('alert').waitFor();
  await page.getByLabel('订单分组描述',{exact:true}).fill(question);await page.getByRole('button',{name:'生成订单分组候选',exact:true}).click();await candidate.waitFor();assert.equal(JSON.parse(await candidate.locator('pre').innerText()).group_by,'day');
  await page.getByRole('link',{name:'受限查询统一入口与能力说明',exact:true}).click();await page.getByLabel('统一查询描述',{exact:true}).fill(question);await page.getByRole('button',{name:'评估问法并生成候选',exact:true}).click();await page.getByRole('heading',{name:'已支持（待授权候选）',exact:true}).waitFor();assert.equal(JSON.parse(await page.getByLabel('统一请求候选',{exact:true}).innerText()).bucket_timezone,'UTC+08:00');
  assert.deepEqual(calls,[]);assert.deepEqual(await page.evaluate(()=>[localStorage.length,sessionStorage.length]),[0,0]);
 }finally{await context.close();await engine.close();}
});
for(const suite of ['counts','money','denials'])test(`按日实际业务闭环 ${suite}：授权、精确分桶、未知完整性和ACK`,async()=>{
 const tokens=JSON.parse(await readFile(profile.tokens,'utf8'));const engine=await browser(),context=await engine.newContext(),page=await context.newPage();page.setDefaultTimeout(5000);
 const samples=suite==='counts'?[{counts:[4,2],recover:true},{zone:'UTC',counts:[6]},{since:'2026-10-01T16:00:00Z',until:'2026-10-02T16:00:00Z',counts:[3],full:true},{since:'2026-10-03T00:00:00Z',until:'2026-10-04T00:00:00Z',counts:[]}]:suite==='money'?[{metric:'completed_amount',totals:['12100','500','200']},{metric:'completed_average',totals:['12100','500','200']},{metric:'completed_average',since:'2026-10-01T16:00:00Z',until:'2026-10-02T16:00:00Z',totals:[null],partialMoney:true},{metric:'completed_amount',since:'2026-10-05T00:00:00Z',until:'2026-10-06T00:00:00Z',totals:['0']},{metric:'completed_amount',since:'2026-10-06T00:00:00Z',until:'2026-10-07T00:00:00Z',incomplete:true}]:[{denied:true,token:tokens.bob},{denied:true,token:tokens.feedback_admin},{denied:true,invalid:'missing'},{denied:true,invalid:'zone'}];
 try{for(const sample of samples){
  const token=sample.token??tokens.alice;await preview(page,sample);await page.getByRole('button',{name:'确认范围并进入订单分组工作台',exact:true}).click();await apply(page,token);await page.getByRole('button',{name:'只读核对写账本',exact:true}).click();await page.getByRole('button',{name:'填入订单分组草稿（不创建）',exact:true}).click();
  if(sample.invalid){const input=page.getByLabel('消息',{exact:true}),args=JSON.parse((await input.inputValue()).replace('!tool analytics.order_groups ',''));if(sample.invalid==='missing')delete args.bucket_timezone;else args.bucket_timezone='Asia/Shanghai';await input.fill('!tool analytics.order_groups '+JSON.stringify(args));}
  const pending=page.waitForResponse(r=>r.url().endsWith('/v1/workbench/create-attempts')&&r.request().method()==='POST');await page.getByRole('button',{name:'登记新 Run（尚未创建）',exact:true}).click();const prepared=await(await pending).json();assert.equal(prepared.status,'PREPARED');await page.getByRole('button',{name:'确认执行已登记创建一次',exact:true}).click();await page.getByRole('button',{name:'预览本人确认收口',exact:true}).waitFor();await page.getByLabel('Run ID',{exact:true}).fill(prepared.run_id);await phase(page,prepared.run_id,'completed',token);
  const events=await(await page.request.get(`${profile.runtime}/v1/runs/${prepared.run_id}/events`,{headers:{Authorization:`Bearer ${token}`}})).json();const results=events.filter(e=>e.type==='tool.result');assert.equal(results.length,1);const panel=page.getByRole('region',{name:'订单分组结果',exact:true});
  if(sample.denied){assert.equal(results[0].payload.status,'error');assert.equal(await panel.count(),0);}
  else{assert.equal(results[0].payload.status,'ok');const out=results[0].payload.output;assert.equal(out.bucket_timezone,sample.zone??'UTC+08:00');assert.equal(out.complete,!sample.incomplete);await panel.waitFor();
   if(sample.incomplete){assert.deepEqual(out.groups,[]);assert.equal(await panel.getByRole('table').count(),0);}
   else{if(sample.counts)assert.deepEqual(out.groups.map(g=>g.order_count),sample.counts);if(sample.totals)assert.deepEqual(out.groups.map(g=>g.total_minor),sample.totals);if(sample.full)assert.equal(out.groups[0].partial_bucket,false);
    if(sample.partialMoney){assert.equal(out.groups[0].known_minor,'200');assert.equal(out.groups[0].amount_coverage,0.5);assert.equal(out.groups[0].average_numerator_minor,null);assert.equal(out.groups[0].average_denominator,null);}
    if(sample.metric==='completed_average'&&!sample.partialMoney){assert.deepEqual(out.groups.map(g=>g.average_denominator),[2,1,1]);}
    assert.equal(await panel.locator('tbody tr').count(),out.groups.length);
   }
   assert.equal(JSON.stringify(out).includes('order_id'),false);assert.equal(JSON.stringify(out).includes('NEVER-MODEL'),false);
   for(const other of [tokens.bob,tokens.feedback_admin,tokens.foreign])assert.equal((await page.request.get(`${profile.runtime}/v1/runs/${prepared.run_id}/events`,{headers:{Authorization:`Bearer ${other}`}})).status(),other===tokens.foreign?404:403);
  }
  if(sample.recover){let writes=0;const track=r=>{if(r.method()==='POST'&&new URL(r.url()).pathname.startsWith('/v1/'))writes++;};page.on('request',track);await page.reload();await preview(page);await page.getByRole('button',{name:'确认范围并进入订单分组工作台',exact:true}).click();await apply(page,token);await page.getByRole('button',{name:'只读核对写账本',exact:true}).click();await page.getByRole('button',{name:'预览本人确认收口',exact:true}).waitFor();assert.equal(writes,0);page.off('request',track);assert.equal(await page.getByRole('button',{name:'登记新 Run（尚未创建）',exact:true}).isDisabled(),true);}
  await page.getByRole('button',{name:'预览本人确认收口',exact:true}).click();await page.getByRole('button',{name:'确认收口此回执',exact:true}).click();await page.waitForFunction(()=>{const b=[...document.querySelectorAll('button')].find(b=>b.textContent?.trim()==='登记新 Run（尚未创建）');return b&&!b.matches(':disabled');});
 }}finally{await context.close();await engine.close();}
});
