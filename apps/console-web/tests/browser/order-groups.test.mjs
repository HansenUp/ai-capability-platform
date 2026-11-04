import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {apply,browser,phase,profile} from './support.mjs';

test('F10金额分组：预览无请求、拒绝跨状态金额/任意分组，编辑重载失效',async()=>{
  const engine=await browser(),context=await engine.newContext(),page=await context.newPage();page.setDefaultTimeout(5000);
  const calls=[];for(const pattern of ['**/console/**','**/v1/**'])await page.route(pattern,async r=>{calls.push(r.request().url());await r.abort();});
  try{
    await page.goto(profile.url+'/manage/order-groups');
    await page.getByRole('button',{name:'预览订单分组查询',exact:true}).click();
    const candidate=page.getByRole('region',{name:'订单分组候选',exact:true});await candidate.waitFor();
    assert.equal(JSON.parse(await candidate.locator('pre').innerText()).metric,'completed_amount');
    assert.equal(await page.getByRole('region',{name:'订单分组执行',exact:true}).count(),0);
    await page.getByLabel('分组当前状态',{exact:true}).selectOption('all');assert.equal(await candidate.count(),0);
    await page.getByRole('button',{name:'预览订单分组查询',exact:true}).click();await page.getByRole('alert').waitFor();
    await page.getByLabel('统计指标',{exact:true}).selectOption('order_count');
    await page.getByLabel('分组维度',{exact:true}).selectOption('status_currency');
    await page.getByRole('button',{name:'预览订单分组查询',exact:true}).click();
    assert.equal(JSON.parse(await candidate.locator('pre').innerText()).group_by,'status_currency');
    await page.getByLabel('订单分组描述',{exact:true}).fill('统计创建时间从“2026-10-01T00:00:00Z”到“2026-10-02T00:00:00Z”的已完成订单金额，按币种分组');
    await page.getByRole('button',{name:'生成订单分组候选',exact:true}).click();assert.equal(JSON.parse(await candidate.locator('pre').innerText()).status,'completed');
    await page.getByLabel('订单分组描述',{exact:true}).fill('统计今天销售额');
    await page.getByRole('button',{name:'生成订单分组候选',exact:true}).click();await page.getByRole('alert').waitFor();assert.equal(await candidate.count(),0);
    await page.reload();assert.equal(await candidate.count(),0);assert.deepEqual(calls,[]);assert.deepEqual(await page.evaluate(()=>[localStorage.length,sessionStorage.length]),[0,0]);
  }finally{await context.close();await engine.close();}
});

test('F10金额分组：分币种、缺失/零/空/拒绝与分组数量，本人ACK和身份隔离',async()=>{
  const tokens=JSON.parse(await readFile(profile.tokens,'utf8'));
  const engine=await browser(),context=await engine.newContext(),page=await context.newPage();page.setDefaultTimeout(5000);
  const cases=[
    {id:'money',day:1,until:2,metric:'completed_amount',status:'completed',group:'currency',n:2},
    {id:'average',day:1,until:2,metric:'completed_average',status:'completed',group:'currency',n:2},
    {id:'average-partial',day:1,until:3,metric:'completed_average',status:'completed',group:'currency',n:2},
    {id:'average-zero',day:5,until:6,metric:'completed_average',status:'completed',group:'currency',n:1},
    {id:'average-empty',day:3,until:4,metric:'completed_average',status:'completed',group:'currency',n:0},
    {id:'partial',day:1,until:3,metric:'completed_amount',status:'completed',group:'currency',n:2},
    {id:'missing',day:2,until:3,metric:'completed_amount',status:'completed',group:'currency',n:1},
    {id:'zero',day:5,until:6,metric:'completed_amount',status:'completed',group:'currency',n:1},
    {id:'empty',day:3,until:4,metric:'completed_amount',status:'completed',group:'currency',n:0},
    {id:'bad-currency',day:6,until:7,metric:'completed_amount',status:'completed',group:'currency',n:0},
    ...['status','currency','status_currency'].map((group,i)=>({id:'count',day:1,until:2,metric:'order_count',status:'all',group,n:i===2?3:2})),
    {id:'denied',day:1,until:2,metric:'completed_amount',status:'completed',group:'currency',token:tokens.bob},
    {id:'denied',day:1,until:2,metric:'completed_amount',status:'completed',group:'currency',token:tokens.feedback_admin},
    {id:'invalid-body',day:1,until:2,metric:'completed_amount',status:'completed',group:'currency'},
  ];
  try{
    for(const sample of cases){
      const token=sample.token??tokens.alice;
      await page.goto(profile.url+'/manage/order-groups');
      await page.getByLabel('分组创建起点（含时区）',{exact:true}).fill(`2026-10-${String(sample.day).padStart(2,'0')}T00:00:00Z`);
      await page.getByLabel('分组创建终点（不含，含时区）',{exact:true}).fill(`2026-10-${String(sample.until).padStart(2,'0')}T00:00:00Z`);
      await page.getByLabel('分组当前状态',{exact:true}).selectOption(sample.status);
      await page.getByLabel('统计指标',{exact:true}).selectOption(sample.metric);await page.getByLabel('分组维度',{exact:true}).selectOption(sample.group);
      await page.getByRole('button',{name:'预览订单分组查询',exact:true}).click();
      await page.getByRole('button',{name:'确认范围并进入订单分组工作台',exact:true}).click();await apply(page,token);
      await page.getByRole('button',{name:'只读核对写账本',exact:true}).click();await page.getByRole('button',{name:'填入订单分组草稿（不创建）',exact:true}).click();
      if(sample.id==='invalid-body'){
        const text=await page.getByLabel('消息',{exact:true}).inputValue();const args=JSON.parse(text.replace('!tool analytics.order_groups ',''));args.status='all';
        await page.getByLabel('消息',{exact:true}).fill('!tool analytics.order_groups '+JSON.stringify(args));
      }
      const pending=page.waitForResponse(r=>r.url().endsWith('/v1/workbench/create-attempts')&&r.request().method()==='POST');
      await page.getByRole('button',{name:'登记新 Run（尚未创建）',exact:true}).click();const prepared=await(await pending).json();assert.equal(prepared.status,'PREPARED');
      await page.getByRole('button',{name:'确认执行已登记创建一次',exact:true}).click();await page.getByRole('button',{name:'预览本人确认收口',exact:true}).waitFor();
      await page.getByLabel('Run ID',{exact:true}).fill(prepared.run_id);await phase(page,prepared.run_id,'completed',token);
      const events=await(await page.request.get(`${profile.runtime}/v1/runs/${prepared.run_id}/events`,{headers:{Authorization:`Bearer ${token}`}})).json();
      const results=events.filter(e=>e.type==='tool.result');assert.equal(results.length,1);
      const panel=page.getByRole('region',{name:'订单分组结果',exact:true});
      if(['denied','invalid-body'].includes(sample.id)){
        assert.equal(results[0].payload.status,'error');assert.equal(await panel.count(),0);
      }else{
        assert.equal(results[0].payload.status,'ok');const output=results[0].payload.output;assert.equal(output.groups.length,sample.n);await panel.waitFor();
        assert.equal(JSON.stringify(output).includes('NEVER-MODEL'),false);assert.equal(JSON.stringify(output).includes('order_id'),false);assert.equal(Object.hasOwn(output,'total_minor'),false);
        if(sample.id==='money')assert.deepEqual(output.groups.map(g=>g.total_minor),['12000','500']);
        if(sample.id==='average'){assert.equal(output.groups[0].average_numerator_minor,'12000');assert.equal(output.groups[0].average_denominator,1);assert.match(await panel.innerText(),/平均金额/);}
        if(sample.id==='average-partial'){assert.equal(output.groups[0].average_numerator_minor,null);assert.equal(output.groups[0].average_denominator,null);assert.match(await panel.innerText(),/均值以完整金额/);}
        if(sample.id==='average-zero')assert.equal(output.groups[0].average_numerator_minor,'0');
        if(sample.id==='average-empty')assert.match(await panel.innerText(),/无分组/);
        if(sample.id==='partial'){assert.equal(output.groups[0].total_minor,null);assert.equal(output.groups[0].known_minor,'12000');assert.equal(output.groups[0].amount_coverage,0.5);assert.match(await panel.innerText(),/正式总额未知/);}
        if(sample.id==='missing'){assert.equal(output.groups[0].total_minor,null);assert.equal(output.groups[0].known_minor,null);}
        if(sample.id==='zero')assert.equal(output.groups[0].total_minor,'0');
        if(sample.id==='empty')assert.match(await panel.innerText(),/无分组/);
        if(sample.id==='bad-currency'){assert.equal(output.complete,false);assert.match(await panel.innerText(),/币种无效/);}
        if(sample.id==='count'){assert.equal(output.groups.reduce((n,g)=>n+g.order_count,0),3);assert.equal(JSON.stringify(output).includes('total_minor'),false);}
        for(const other of [tokens.bob,tokens.foreign,tokens.feedback_admin]){
          const denied=await page.request.get(`${profile.runtime}/v1/runs/${prepared.run_id}/events`,{headers:{Authorization:`Bearer ${other}`}});assert.equal(denied.status(),other===tokens.foreign?404:403);
        }
      }
      if(sample.id==='money'){
        let writes=0;const track=r=>{if(r.method()==='POST'&&new URL(r.url()).pathname.startsWith('/v1/'))writes++;};page.on('request',track);
        await page.reload();assert.equal(await page.getByRole('region',{name:'订单分组执行',exact:true}).count(),0);
        await page.getByRole('button',{name:'预览订单分组查询',exact:true}).click();await page.getByRole('button',{name:'确认范围并进入订单分组工作台',exact:true}).click();
        await apply(page,token);await page.getByRole('button',{name:'只读核对写账本',exact:true}).click();await page.getByRole('button',{name:'预览本人确认收口',exact:true}).waitFor();
        assert.equal(writes,0);page.off('request',track);assert.equal(await page.getByRole('button',{name:'登记新 Run（尚未创建）',exact:true}).isDisabled(),true);
      }
      await page.getByRole('button',{name:'预览本人确认收口',exact:true}).click();await page.getByRole('button',{name:'确认收口此回执',exact:true}).click();
      await page.waitForFunction(()=>{const b=[...document.querySelectorAll('button')].find(b=>b.textContent?.trim()==='登记新 Run（尚未创建）');return b&&!b.matches(':disabled');});
    }
  }finally{await context.close();await engine.close();}
});


test('F10权限：原数量Agent不能调用金额分组工具',async()=>{
  const tokens=JSON.parse(await readFile(profile.tokens,'utf8'));
  const engine=await browser(),context=await engine.newContext(),page=await context.newPage();page.setDefaultTimeout(5000);
  try{
    await page.goto(profile.url+'/manage/order-counts');await page.getByRole('button',{name:'预览订单数量查询',exact:true}).click();
    await page.getByRole('button',{name:'确认范围并进入订单统计工作台',exact:true}).click();await apply(page,tokens.alice);
    await page.getByRole('button',{name:'只读核对写账本',exact:true}).click();await page.getByRole('button',{name:'填入订单统计草稿（不创建）',exact:true}).click();
    await page.getByLabel('消息',{exact:true}).fill('!tool analytics.order_groups '+JSON.stringify({since:'2026-10-01T00:00:00Z',until:'2026-10-02T00:00:00Z',status:'completed',metric:'completed_amount',group_by:'currency'}));
    const pending=page.waitForResponse(r=>r.url().endsWith('/v1/workbench/create-attempts')&&r.request().method()==='POST');
    await page.getByRole('button',{name:'登记新 Run（尚未创建）',exact:true}).click();const prepared=await(await pending).json();assert.equal(prepared.status,'PREPARED');
    await page.getByRole('button',{name:'确认执行已登记创建一次',exact:true}).click();await page.getByRole('button',{name:'预览本人确认收口',exact:true}).waitFor();
    await page.getByLabel('Run ID',{exact:true}).fill(prepared.run_id);await phase(page,prepared.run_id,'completed',tokens.alice);
    const events=await(await page.request.get(`${profile.runtime}/v1/runs/${prepared.run_id}/events`,{headers:{Authorization:`Bearer ${tokens.alice}`}})).json();
    assert.equal(events.some(e=>e.type==='tool.result'&&e.payload.status==='ok'),false);
    assert.equal(events.some(e=>e.type==='tool.result'&&e.payload.status==='error'),true);
    assert.equal(await page.getByRole('region',{name:'订单分组结果',exact:true}).count(),0);
    await page.getByRole('button',{name:'预览本人确认收口',exact:true}).click();await page.getByRole('button',{name:'确认收口此回执',exact:true}).click();
  }finally{await context.close();await engine.close();}
});
