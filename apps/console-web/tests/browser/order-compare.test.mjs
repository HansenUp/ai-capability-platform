import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {apply,browser,phase,profile} from './support.mjs';
const question='对比基准创建时间从“2026-10-01T00:00:00Z”到“2026-10-02T00:00:00Z”与当前创建时间从“2026-10-02T00:00:00Z”到“2026-10-03T00:00:00Z”的全部订单数量';
async function preview(page,{baseline='01',current='02',status='all',millis=false}={}){
 await page.goto(profile.url+'/manage/order-compare');
 for(const [name,value] of [['基准创建起点（含时区）',`2026-10-${baseline}T00:00:00Z`],['基准创建终点（不含，含时区）',`2026-10-${String(Number(baseline)+1).padStart(2,'0')}T00:00:00Z`],['当前创建起点（含时区）',`2026-10-${current}T00:00:00Z`],['当前创建终点（不含，含时区）',`2026-10-${String(Number(current)+1).padStart(2,'0')}T00:00:00Z`]])await page.getByLabel(name,{exact:true}).fill(value);
 if(millis)for(const name of ['基准创建起点（含时区）','基准创建终点（不含，含时区）','当前创建起点（含时区）','当前创建终点（不含，含时区）']){const input=page.getByLabel(name,{exact:true});await input.fill((await input.inputValue()).replace('00:00:00Z','00:00:00.001Z'));}
 await page.getByLabel('对比共同状态',{exact:true}).selectOption(status);await page.getByRole('button',{name:'预览订单双窗对比',exact:true}).click();
}
test('双窗对比预览：明确角色、等长不重叠、自然语言与统一入口零业务请求',async()=>{
 const engine=await browser(),context=await engine.newContext(),page=await context.newPage();page.setDefaultTimeout(5000);const calls=[];
 for(const pattern of ['**/console/**','**/v1/**'])await page.route(pattern,async r=>{calls.push(r.request().url());await r.abort();});
 try{
  await preview(page);const candidate=page.getByRole('region',{name:'订单对比候选',exact:true});await candidate.waitFor();assert.equal(JSON.parse(await candidate.locator('pre').innerText()).status,'all');
  await page.getByLabel('当前创建终点（不含，含时区）',{exact:true}).fill('2026-10-04T00:00:00Z');assert.equal(await candidate.count(),0);await page.getByRole('button',{name:'预览订单双窗对比',exact:true}).click();await page.getByRole('alert').waitFor();
  await page.getByLabel('订单对比描述',{exact:true}).fill(question);await page.getByRole('button',{name:'生成订单对比候选',exact:true}).click();await candidate.waitFor();assert.match(await candidate.innerText(),/基准为0/);
  await page.getByRole('link',{name:'受限查询统一入口与能力说明',exact:true}).click();await page.getByLabel('统一查询描述',{exact:true}).fill(question);await page.getByRole('button',{name:'评估问法并生成候选',exact:true}).click();await page.getByRole('heading',{name:'已支持（待授权候选）',exact:true}).waitFor();assert.equal(await page.getByRole('link',{name:'打开对应查询页核对并确认',exact:true}).getAttribute('href'),'/manage/order-compare');
  assert.deepEqual(calls,[]);assert.deepEqual(await page.evaluate(()=>[localStorage.length,sessionStorage.length]),[0,0]);
  await page.getByRole('link',{name:'打开对应查询页核对并确认',exact:true}).click();await page.getByRole('heading',{name:'双窗口订单数量对比 · 合成隔离查询',exact:true}).waitFor();assert.equal(await candidate.count(),0);assert.equal(await page.getByRole('region',{name:'订单对比执行',exact:true}).count(),0);
 }finally{await context.close();await engine.close();}
});
for(const suite of ['counts','denials'])test(`双窗对比实际闭环 ${suite}：精确差值、零基数、权限、恢复与ACK`,async()=>{
 const tokens=JSON.parse(await readFile(profile.tokens,'utf8'));const engine=await browser(),context=await engine.newContext(),page=await context.newPage();page.setDefaultTimeout(5000);
 const samples=suite==='counts'?[
  {b:3,c:1,recover:true},{millis:true,b:2,c:1},{status:'completed',b:2,c:1},{status:'pending',b:1,c:0},{status:'cancelled',b:0,c:0},
  {baseline:'03',current:'05',b:0,c:1},{baseline:'05',current:'06',b:1,c:1},
 ]:[{denied:true,token:tokens.bob},{denied:true,token:tokens.feedback_admin},{denied:true,invalid:'overlap'},{denied:true,invalid:'extra'}];
 try{for(const sample of samples){
  const token=sample.token??tokens.alice;await preview(page,sample);await page.getByRole('button',{name:'确认范围并进入订单对比工作台',exact:true}).click();await apply(page,token);await page.getByRole('button',{name:'只读核对写账本',exact:true}).click();await page.getByRole('button',{name:'填入订单对比草稿（不创建）',exact:true}).click();
  if(sample.invalid){const input=page.getByLabel('消息',{exact:true}),args=JSON.parse((await input.inputValue()).replace('!tool analytics.order_compare ',''));if(sample.invalid==='overlap'){args.current_since=args.baseline_since;args.current_until=args.baseline_until;}else args.current_status='completed';await input.fill('!tool analytics.order_compare '+JSON.stringify(args));}
  const pending=page.waitForResponse(r=>r.url().endsWith('/v1/workbench/create-attempts')&&r.request().method()==='POST');await page.getByRole('button',{name:'登记新 Run（尚未创建）',exact:true}).click();const prepared=await(await pending).json();assert.equal(prepared.status,'PREPARED');await page.getByRole('button',{name:'确认执行已登记创建一次',exact:true}).click();await page.getByRole('button',{name:'预览本人确认收口',exact:true}).waitFor();await page.getByLabel('Run ID',{exact:true}).fill(prepared.run_id);await phase(page,prepared.run_id,'completed',token);
  const events=await(await page.request.get(`${profile.runtime}/v1/runs/${prepared.run_id}/events`,{headers:{Authorization:`Bearer ${token}`}})).json();const results=events.filter(e=>e.type==='tool.result');assert.equal(results.length,1);const panel=page.getByRole('region',{name:'订单双窗对比结果',exact:true});
  if(sample.denied){assert.equal(results[0].payload.status,'error');assert.equal(await panel.count(),0);}
  else{assert.equal(results[0].payload.status,'ok');const out=results[0].payload.output;assert.equal(out.complete,true);assert.equal(out.baseline_count,sample.b);assert.equal(out.current_count,sample.c);assert.equal(out.delta_count,sample.c-sample.b);assert.equal(out.relative_numerator,sample.b?sample.c-sample.b:null);assert.equal(out.relative_denominator,sample.b||null);await panel.waitFor();assert.equal(await panel.locator('tbody tr').count(),1);if(!sample.b)assert.match(await panel.innerText(),/基准数量为0，相对变化未知/);else assert.match(await panel.innerText(),new RegExp(`${sample.c-sample.b} / ${sample.b}`));
   assert.equal(JSON.stringify(out).includes('order_id'),false);assert.equal(JSON.stringify(out).includes('NEVER-MODEL'),false);
   for(const other of [tokens.bob,tokens.feedback_admin,tokens.foreign])assert.equal((await page.request.get(`${profile.runtime}/v1/runs/${prepared.run_id}/events`,{headers:{Authorization:`Bearer ${other}`}})).status(),other===tokens.foreign?404:403);
  }
  if(sample.recover){let writes=0;const track=r=>{if(r.method()==='POST'&&new URL(r.url()).pathname.startsWith('/v1/'))writes++;};page.on('request',track);await page.reload();await preview(page);await page.getByRole('button',{name:'确认范围并进入订单对比工作台',exact:true}).click();await apply(page,token);await page.getByRole('button',{name:'只读核对写账本',exact:true}).click();await page.getByRole('button',{name:'预览本人确认收口',exact:true}).waitFor();assert.equal(writes,0);page.off('request',track);assert.equal(await page.getByRole('button',{name:'登记新 Run（尚未创建）',exact:true}).isDisabled(),true);}
  await page.getByRole('button',{name:'预览本人确认收口',exact:true}).click();await page.getByRole('button',{name:'确认收口此回执',exact:true}).click();await page.waitForFunction(()=>{const b=[...document.querySelectorAll('button')].find(b=>b.textContent?.trim()==='登记新 Run（尚未创建）');return b&&!b.matches(':disabled');});
 }}finally{await context.close();await engine.close();}
});


test('旧数量与分组Agent不能调用新对比工具',async()=>{
 const tokens=JSON.parse(await readFile(profile.tokens,'utf8'));const engine=await browser(),context=await engine.newContext(),page=await context.newPage();page.setDefaultTimeout(5000);
 try{for(const groups of [false,true]){
  await page.goto(profile.url+(groups?'/manage/order-groups':'/manage/order-counts'));
  await page.getByRole('button',{name:groups?'预览订单分组查询':'预览订单数量查询',exact:true}).click();await page.getByRole('button',{name:groups?'确认范围并进入订单分组工作台':'确认范围并进入订单统计工作台',exact:true}).click();await apply(page,tokens.alice);await page.getByRole('button',{name:'只读核对写账本',exact:true}).click();
  const args={baseline_since:'2026-10-01T00:00:00Z',baseline_until:'2026-10-02T00:00:00Z',current_since:'2026-10-02T00:00:00Z',current_until:'2026-10-03T00:00:00Z',status:'all'};
  await page.getByLabel('消息',{exact:true}).fill('!tool analytics.order_compare '+JSON.stringify(args));
  const pending=page.waitForResponse(r=>r.url().endsWith('/v1/workbench/create-attempts')&&r.request().method()==='POST');await page.getByRole('button',{name:'登记新 Run（尚未创建）',exact:true}).click();const prepared=await(await pending).json();assert.equal(prepared.status,'PREPARED');await page.getByRole('button',{name:'确认执行已登记创建一次',exact:true}).click();await page.getByRole('button',{name:'预览本人确认收口',exact:true}).waitFor();await page.getByLabel('Run ID',{exact:true}).fill(prepared.run_id);await phase(page,prepared.run_id,'completed',tokens.alice);
  const events=await(await page.request.get(`${profile.runtime}/v1/runs/${prepared.run_id}/events`,{headers:{Authorization:`Bearer ${tokens.alice}`}})).json();assert.equal(events.some(e=>e.type==='tool.result'&&e.payload.status==='ok'),false);assert.equal(events.some(e=>e.type==='tool.result'&&e.payload.status==='error'),true);assert.equal(await page.getByRole('region',{name:'订单双窗对比结果',exact:true}).count(),0);
  await page.getByRole('button',{name:'预览本人确认收口',exact:true}).click();await page.getByRole('button',{name:'确认收口此回执',exact:true}).click();
 }}finally{await context.close();await engine.close();}
});
