import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {apply,browser,phase,profile} from './support.mjs';
test('双窗对比：实际超100行来源不提供两窗部分数量',async()=>{
 const tokens=JSON.parse(await readFile(profile.tokens,'utf8'));const engine=await browser(),context=await engine.newContext(),page=await context.newPage();page.setDefaultTimeout(5000);
 try{
  await page.goto(profile.url+'/manage/order-compare');
  await page.getByRole('button',{name:'预览订单双窗对比',exact:true}).click();await page.getByRole('button',{name:'确认范围并进入订单对比工作台',exact:true}).click();await apply(page,tokens.alice);
  await page.getByRole('button',{name:'只读核对写账本',exact:true}).click();await page.getByRole('button',{name:'填入订单对比草稿（不创建）',exact:true}).click();
  const pending=page.waitForResponse(r=>r.url().endsWith('/v1/workbench/create-attempts')&&r.request().method()==='POST');await page.getByRole('button',{name:'登记新 Run（尚未创建）',exact:true}).click();const prepared=await(await pending).json();assert.equal(prepared.status,'PREPARED');
  await page.getByRole('button',{name:'确认执行已登记创建一次',exact:true}).click();await page.getByRole('button',{name:'预览本人确认收口',exact:true}).waitFor();await page.getByLabel('Run ID',{exact:true}).fill(prepared.run_id);await phase(page,prepared.run_id,'completed',tokens.alice);
  const events=await(await page.request.get(`${profile.runtime}/v1/runs/${prepared.run_id}/events`,{headers:{Authorization:`Bearer ${tokens.alice}`}})).json();const result=events.find(e=>e.type==='tool.result');assert.equal(result.payload.status,'ok');
  assert.equal(result.payload.output.complete,false);for(const k of ['baseline_count','current_count','delta_count','relative_numerator','relative_denominator'])assert.equal(result.payload.output[k],null);
  const panel=page.getByRole('region',{name:'订单双窗对比结果',exact:true});await panel.waitFor();assert.match(await panel.innerText(),/来源截断或范围不完整/);assert.equal(await panel.getByRole('table').count(),0);
  await page.getByRole('button',{name:'预览本人确认收口',exact:true}).click();await page.getByRole('button',{name:'确认收口此回执',exact:true}).click();
 }finally{await context.close();await engine.close();}
});
