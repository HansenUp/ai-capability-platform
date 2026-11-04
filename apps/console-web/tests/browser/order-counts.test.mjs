import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {apply,browser,phase,profile} from './support.mjs';

test('F10日期状态：预览校验、明确时间与状态、编辑重载清空且不发请求', async () => {
  const engine=await browser(),context=await engine.newContext(),page=await context.newPage();
  page.setDefaultTimeout(5000);
  const calls=[];
  for (const pattern of ['**/console/**','**/v1/**']) await page.route(pattern, async route => {calls.push(route.request().url());await route.abort();});
  try {
    await page.goto(profile.url+'/manage/order-counts');
    await page.getByLabel('创建起点（含时区）',{exact:true}).fill('2026-02-30T00:00:00Z');
    await page.getByRole('button',{name:'预览订单数量查询',exact:true}).click();await page.getByRole('alert').waitFor();
    const candidate=page.getByRole('region',{name:'订单统计候选',exact:true});assert.equal(await candidate.count(),0);
    await page.getByLabel('订单统计描述',{exact:true}).fill('统计创建时间从“2026-10-01T08:00:00+08:00”到“2026-10-02T00:00:00Z”的已完成订单数量');
    await page.getByRole('button',{name:'生成订单统计候选',exact:true}).click();
    assert.deepEqual(JSON.parse(await candidate.locator('pre').innerText()),{since:'2026-10-01T00:00:00.000Z',until:'2026-10-02T00:00:00.000Z',status:'completed'});
    assert.equal(await page.getByRole('region',{name:'订单统计执行',exact:true}).count(),0);
    await page.getByLabel('当前订单状态',{exact:true}).selectOption('pending');assert.equal(await candidate.count(),0);
    await page.getByLabel('订单统计描述',{exact:true}).fill('统计今天销售额');
    await page.getByRole('button',{name:'生成订单统计候选',exact:true}).click();await page.getByRole('alert').waitFor();
    assert.equal(await candidate.count(),0);await page.reload();assert.equal(await candidate.count(),0);
    assert.deepEqual(calls,[]);assert.deepEqual(await page.evaluate(()=>[localStorage.length,sessionStorage.length]),[0,0]);
  } finally {await context.close();await engine.close();}
});

test('F10日期状态：权威数量、端点/空结果、状态、原人ACK及Bob拒绝', async () => {
  const tokens=JSON.parse(await readFile(profile.tokens,'utf8'));
  const engine=await browser(),context=await engine.newContext(),page=await context.newPage();page.setDefaultTimeout(5000);
  const headers={Authorization:`Bearer ${tokens.alice}`};
  try {
    for(const [status,since,until,count,token] of [
      ['all','2026-10-01T08:00:00+08:00','2026-10-02T00:00:00Z',3,tokens.alice],
      ['completed','2026-10-01T00:00:00Z','2026-10-02T00:00:00Z',2,tokens.alice],
      ['pending','2026-10-01T00:00:00Z','2026-10-02T00:00:00Z',1,tokens.alice],
      ['cancelled','2026-10-01T00:00:00Z','2026-10-02T00:00:00Z',0,tokens.alice],
      ['all','2026-10-03T00:00:00Z','2026-10-04T00:00:00Z',0,tokens.alice],
      ['all','2026-10-02T09:00:00Z','2026-10-02T09:00:01Z',1,tokens.alice],
      ['all','2026-10-01T00:00:00Z','2026-10-02T00:00:00Z',null,tokens.bob],
      ['all','2026-10-01T00:00:00Z','2026-10-02T00:00:00Z',null,tokens.feedback_admin],
    ]) {
      await page.goto(profile.url+'/manage/order-counts');
      await page.getByLabel('创建起点（含时区）',{exact:true}).fill(since);
      await page.getByLabel('创建终点（不含，含时区）',{exact:true}).fill(until);
      await page.getByLabel('当前订单状态',{exact:true}).selectOption(status);
      await page.getByRole('button',{name:'预览订单数量查询',exact:true}).click();
      await page.getByRole('button',{name:'确认范围并进入订单统计工作台',exact:true}).click();
      await apply(page,token);
      await page.getByRole('button',{name:'只读核对写账本',exact:true}).click();
      await page.getByRole('button',{name:'填入订单统计草稿（不创建）',exact:true}).click();
      const pending=page.waitForResponse(r=>r.url().endsWith('/v1/workbench/create-attempts')&&r.request().method()==='POST');
      await page.getByRole('button',{name:'登记新 Run（尚未创建）',exact:true}).click();
      const prepared=await(await pending).json();assert.equal(prepared.status,'PREPARED');
      await page.getByRole('button',{name:'确认执行已登记创建一次',exact:true}).click();
      await page.getByRole('button',{name:'预览本人确认收口',exact:true}).waitFor();
      await page.getByLabel('Run ID',{exact:true}).fill(prepared.run_id);await phase(page,prepared.run_id,'completed',token);
      const stored=await(await page.request.get(`${profile.runtime}/v1/runs/${prepared.run_id}`,{headers:{Authorization:`Bearer ${token}`}})).json();assert.equal(stored.agent_id,'f10-order-count');
      const events=await(await page.request.get(`${profile.runtime}/v1/runs/${prepared.run_id}/events`,{headers:{Authorization:`Bearer ${token}`}})).json();
      const results=events.filter(e=>e.type==='tool.result');assert.equal(results.length,1);
      const result=page.getByRole('region',{name:'订单数量结果',exact:true});
      if(count===null) {assert.equal(results[0].payload.status,'error');assert.equal(await result.count(),0);}
      else {
        assert.equal(results[0].payload.status,'ok');assert.equal(results[0].payload.output.order_count,count);
        assert.equal(results[0].payload.output.status_filter,status);
        assert.equal(JSON.stringify(results[0].payload.output).includes('amount_minor'),false);
        assert.equal(JSON.stringify(results[0].payload.output).includes('NEVER-MODEL'),false);
        assert.equal(JSON.stringify(results[0].payload.output).includes('order_id'),false);
        await result.waitFor();assert.match(await result.innerText(),new RegExp(`所选范围订单数：${count}`));
        for(const other of [tokens.bob,tokens.foreign,tokens.feedback_admin]) {
          if(!other) continue;
          const denied=await page.request.get(`${profile.runtime}/v1/runs/${prepared.run_id}/events`,{headers:{Authorization:`Bearer ${other}`}});
          assert.equal(denied.status(),other===tokens.foreign?404:403);
        }
      }
      if(status==='all' && count===3) {
        let writes=0;
        const track=r=>{if(r.method()==='POST'&&new URL(r.url()).pathname.startsWith('/v1/'))writes++;};
        page.on('request',track);
        await page.reload();
        assert.equal(await page.getByRole('region',{name:'订单统计执行',exact:true}).count(),0);
        await page.getByRole('button',{name:'预览订单数量查询',exact:true}).click();
        await page.getByRole('button',{name:'确认范围并进入订单统计工作台',exact:true}).click();
        await apply(page,token);
        await page.getByRole('button',{name:'只读核对写账本',exact:true}).click();
        await page.getByRole('button',{name:'预览本人确认收口',exact:true}).waitFor();
        assert.equal(writes,0);page.off('request',track);
        assert.equal(await page.getByRole('button',{name:'登记新 Run（尚未创建）',exact:true}).isDisabled(),true);
      }
      await page.getByRole('button',{name:'预览本人确认收口',exact:true}).click();
      await page.getByRole('button',{name:'确认收口此回执',exact:true}).click();
      await page.waitForFunction(()=>{const b=[...document.querySelectorAll('button')].find(b=>b.textContent?.trim()==='登记新 Run（尚未创建）');return b&&!b.matches(':disabled');});
      const ledger=await(await page.request.get(profile.runtime+'/v1/workbench/attempts',{headers:{Authorization:`Bearer ${token}`}})).json();assert.equal(ledger.active,null);
    }
  } finally {await context.close();await engine.close();}
});
