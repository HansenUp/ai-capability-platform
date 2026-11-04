import test from 'node:test';
import assert from 'node:assert/strict';
import {browser,profile} from './support.mjs';

test('统一查询入口：能力导航、四类评估、候选与执行隔离',async()=>{
 const engine=await browser(),context=await engine.newContext(),page=await context.newPage();page.setDefaultTimeout(5000);
 const calls=[];for(const pattern of ['**/console/**','**/v1/**'])await page.route(pattern,async r=>{calls.push(r.request().url());await r.abort();});
 try{
  await page.goto(profile.url+'/manage/queries');
  const table=page.getByRole('table',{name:'受限查询能力表',exact:true});await table.waitFor();assert.equal(await table.locator('tbody tr').count(),3);
  const input=page.getByLabel('统一查询描述',{exact:true}),button=page.getByRole('button',{name:'评估问法并生成候选',exact:true});
  const result=page.getByRole('region',{name:'统一问法评估',exact:true});
  for(const [text,status] of [['查一下商品a1的订单，最多10条','已支持（待授权候选）'],['统计今天订单数量','需补充信息'],['统计退款金额','待开发或尚未支持'],['统计所有租户订单数量','权限范围拒绝']]){
   await input.fill(text);assert.equal(await result.count(),0);await button.click();await result.getByRole('heading',{name:status,exact:true}).waitFor();
   assert.equal(await result.getByLabel('统一请求候选',{exact:true}).count(),status.startsWith('已支持')?1:0);
  }
  await page.getByRole('button',{name:'填入金额与受限分组示例',exact:true}).click();await button.click();
  assert.equal(await result.getByRole('link',{name:'打开对应查询页核对并确认',exact:true}).getAttribute('href'),'/manage/order-groups');
  await result.getByRole('link',{name:'打开对应查询页核对并确认',exact:true}).click();
  await page.getByRole('heading',{name:'订单金额与分组 · 合成隔离查询',exact:true}).waitFor();
  assert.equal(await page.getByRole('region',{name:'订单分组候选',exact:true}).count(),0);
  assert.equal(await page.getByRole('region',{name:'订单分组执行',exact:true}).count(),0);
  await page.getByRole('link',{name:'受限查询统一入口与能力说明',exact:true}).click();
  assert.equal(await input.inputValue(),'');assert.equal(await result.count(),0);
  await page.reload();assert.equal(await input.inputValue(),'');
  assert.deepEqual(calls,[]);assert.deepEqual(await page.evaluate(()=>[localStorage.length,sessionStorage.length]),[0,0]);
 }finally{await context.close();await engine.close();}
});
