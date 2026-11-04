import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {browser,profile} from './support.mjs';

test('钉钉合成单聊：接收、本人恢复、重载和身份隔离，不自动创建任务',async()=>{
 const engine=await browser(), context=await engine.newContext(), page=await context.newPage();page.setDefaultTimeout(5000);
 const tokens=JSON.parse(await readFile(profile.tokens,'utf8'));
 const id=`im-${Date.now()}`;const writes=[];
 page.on('request',r=>{if(r.method()==='POST')writes.push(new URL(r.url()).pathname);});
 async function apply(user=tokens.alice){
  await page.getByLabel('合成来源令牌',{exact:true}).fill(tokens.im_source);
  await page.getByLabel('本人用户令牌',{exact:true}).fill(user);
  await page.getByRole('button',{name:'应用身份并清空接收结果',exact:true}).click();
 }
 try{
  await page.goto(profile.url+'/im-inbox');await apply();
  await page.getByLabel('事件ID',{exact:true}).fill(id);
  await page.getByRole('button',{name:'显式模拟接收一次',exact:true}).click();
  await page.getByRole('status').filter({hasText:'合成消息已接收'}).waitFor();
  assert.equal(await page.getByRole('link',{name:'进入工作台，重新认证并显式登记任务',exact:true}).count(),0);
  assert.equal(await page.getByRole('button',{name:'显式模拟接收一次',exact:true}).isDisabled(),true);
  await page.getByRole('button',{name:'本人只读核对原事件',exact:true}).click();
  await page.getByRole('article',{name:'本人接收回执'}).waitFor();
  const receipt=await page.getByRole('article',{name:'本人接收回执'}).innerText();
  await page.reload();await apply();await page.getByLabel('事件ID',{exact:true}).fill(id);
  await page.getByRole('button',{name:'本人只读核对原事件',exact:true}).click();
  await page.getByRole('article',{name:'本人接收回执'}).waitFor();
  assert.equal(await page.getByRole('article',{name:'本人接收回执'}).innerText(),receipt);
  await apply(tokens.bob);
  assert.equal(await page.getByRole('article',{name:'本人接收回执'}).count(),0);
  await page.getByRole('button',{name:'本人只读核对原事件',exact:true}).click();
  await page.getByRole('status').filter({hasText:'HTTP 403'}).waitFor();
  assert.equal(await page.getByRole('article',{name:'本人接收回执'}).count(),0);
  await apply();await page.getByRole('button',{name:'本人只读核对原事件',exact:true}).click();
  await page.getByRole('article',{name:'本人接收回执'}).waitFor();
  await page.getByRole('link',{name:'进入工作台，重新认证并显式登记任务',exact:true}).click();
  await page.getByRole('button',{name:'登记新 Run（尚未创建）',exact:true}).waitFor();
  assert.deepEqual(writes,['/v1/isolated-im/events']);
 }finally{await context.close();await engine.close();}
});

test('钉钉接收丢响应仅恢复；旧身份读取响应不覆盖新身份',async()=>{
 const engine=await browser(), context=await engine.newContext(), page=await context.newPage();page.setDefaultTimeout(5000);
 const tokens=JSON.parse(await readFile(profile.tokens,'utf8'));const id=`lost-${Date.now()}`;let sends=0;
 async function apply(user=tokens.alice){await page.getByLabel('合成来源令牌',{exact:true}).fill(tokens.im_source);await page.getByLabel('本人用户令牌',{exact:true}).fill(user);await page.getByRole('button',{name:'应用身份并清空接收结果',exact:true}).click();}
 try{
  await page.route('**/v1/isolated-im/events',async route=>{sends++;const response=await route.fetch();assert.equal(response.status(),200);await route.abort();});
  await page.goto(profile.url+'/im-inbox');await apply();await page.getByLabel('事件ID',{exact:true}).fill(id);
  await page.getByRole('button',{name:'显式模拟接收一次',exact:true}).click();
  await page.getByRole('status').filter({hasText:'接收结果未确认'}).waitFor();
  assert.equal(await page.getByRole('button',{name:'显式模拟接收一次',exact:true}).isDisabled(),true);
  await page.getByRole('button',{name:'本人只读核对原事件',exact:true}).click();await page.getByRole('article',{name:'本人接收回执'}).waitFor();
  let release, entered;const blocked=new Promise(r=>release=r), ready=new Promise(r=>entered=r);
  await page.route(`**/v1/isolated-im/events/${id}?*`,async route=>{const response=await route.fetch();entered();await blocked;await route.fulfill({response}).catch(()=>{});});
  await page.getByRole('button',{name:'本人只读核对原事件',exact:true}).click();await ready;
  await apply(tokens.bob);release();
  await page.unrouteAll({behavior:'wait'});
  assert.equal(await page.getByRole('article',{name:'本人接收回执'}).count(),0);assert.equal(sends,1);
 }finally{await context.close();await engine.close();}
});

test('钉钉身份自然到期清空正文和回执，在途旧响应不能恢复显示',async()=>{
 const engine=await browser(), context=await engine.newContext(), page=await context.newPage();page.setDefaultTimeout(5000);
 const tokens=JSON.parse(await readFile(profile.tokens,'utf8'));const id=`expiry-${Date.now()}`;const writes=[];
 page.on('request',r=>{if(r.method()==='POST'&&new URL(r.url()).pathname.startsWith('/v1/'))writes.push(r.url());});
 try{
  await page.clock.install({time:new Date()});await page.goto(profile.url+'/im-inbox');
  await page.getByLabel('合成来源令牌',{exact:true}).fill(tokens.im_source_short_lived);await page.getByLabel('本人用户令牌',{exact:true}).fill(tokens.alice_short_lived);
  await page.getByRole('button',{name:'应用身份并清空接收结果',exact:true}).click();await page.getByLabel('事件ID',{exact:true}).fill(id);
  await page.getByRole('button',{name:'显式模拟接收一次',exact:true}).click();await page.getByRole('status').filter({hasText:'合成消息已接收'}).waitFor();
  await page.getByRole('button',{name:'本人只读核对原事件',exact:true}).click();await page.getByRole('article',{name:'本人接收回执'}).waitFor();
  let release,entered;const blocked=new Promise(r=>release=r),ready=new Promise(r=>entered=r);
  await page.route(`**/v1/isolated-im/events/${id}?*`,async route=>{const response=await route.fetch();entered();await blocked;await route.fulfill({response}).catch(()=>{});});
  await page.getByRole('button',{name:'本人只读核对原事件',exact:true}).click();await ready;
  const expiry=Math.min(...[tokens.alice_short_lived,tokens.im_source_short_lived].map(t=>JSON.parse(Buffer.from(t.split('.')[1],'base64url').toString()).exp*1000));
  await page.evaluate(()=>{globalThis.__imExpiryPageMarker='same-page';});
  const now=await page.evaluate(()=>Date.now());await page.clock.fastForward(expiry-now+2000);
  try{await page.getByRole('status').filter({hasText:'测试身份已过期'}).waitFor();}
  catch(error){console.log('IM_EXPIRY_DIAGNOSTIC',JSON.stringify({now:await page.evaluate(()=>Date.now()),expiry,status:await page.getByRole('status').innerText(),text:await page.getByLabel('合成消息',{exact:true}).inputValue(),marker:await page.evaluate(()=>globalThis.__imExpiryPageMarker??null),readDisabled:await page.getByRole('button',{name:'本人只读核对原事件',exact:true}).isDisabled()}));release();throw error;}
  release();await page.unrouteAll({behavior:'wait'});
  assert.equal(await page.getByRole('article',{name:'本人接收回执'}).count(),0);
  assert.equal(await page.getByLabel('合成消息',{exact:true}).inputValue(),'');
  assert.equal(await page.getByRole('button',{name:'本人只读核对原事件',exact:true}).isDisabled(),true);
  assert.equal(writes.length,1);
 }finally{await context.close();await engine.close();}
});

test('钉钉当前身份401清空凭据与回执且不自动重发',async()=>{
 const engine=await browser(),context=await engine.newContext(),page=await context.newPage();page.setDefaultTimeout(5000);
 const tokens=JSON.parse(await readFile(profile.tokens,'utf8'));let posts=0;
 page.on('request',r=>{if(r.method()==='POST'&&new URL(r.url()).pathname.startsWith('/v1/'))posts++;});
 try{
  await page.goto(profile.url+'/im-inbox');
  await page.getByLabel('合成来源令牌',{exact:true}).fill(tokens.im_source);await page.getByLabel('本人用户令牌',{exact:true}).fill(tokens.alice);
  await page.getByRole('button',{name:'应用身份并清空接收结果',exact:true}).click();await page.getByLabel('事件ID',{exact:true}).fill('unauthorized-fixture');
  await page.route('**/v1/isolated-im/events/unauthorized-fixture?*',route=>route.fulfill({status:401,json:{detail:'synthetic expired identity'}}));
  await page.getByRole('button',{name:'本人只读核对原事件',exact:true}).click();await page.getByRole('status').filter({hasText:'测试身份已失效'}).waitFor();
  assert.equal(await page.getByRole('article',{name:'本人接收回执'}).count(),0);
  assert.equal(await page.getByRole('button',{name:'显式模拟接收一次',exact:true}).isDisabled(),true);
  assert.equal(await page.getByRole('button',{name:'本人只读核对原事件',exact:true}).isDisabled(),true);assert.equal(posts,0);
 }finally{await context.close();await engine.close();}
});

test('钉钉旧身份迟到401不能注销新身份',async()=>{
 const engine=await browser(),context=await engine.newContext(),page=await context.newPage();page.setDefaultTimeout(5000);
 const tokens=JSON.parse(await readFile(profile.tokens,'utf8'));
 const apply=async user=>{await page.getByLabel('合成来源令牌',{exact:true}).fill(tokens.im_source);await page.getByLabel('本人用户令牌',{exact:true}).fill(user);await page.getByRole('button',{name:'应用身份并清空接收结果',exact:true}).click();};
 try{
  let release,entered;const blocked=new Promise(r=>release=r),ready=new Promise(r=>entered=r);
  await page.goto(profile.url+'/im-inbox');await apply(tokens.alice);await page.getByLabel('事件ID',{exact:true}).fill('late-unauthorized-fixture');
  await page.route('**/v1/isolated-im/events/late-unauthorized-fixture?*',async route=>{entered();await blocked;await route.fulfill({status:401,json:{detail:'old identity'}}).catch(()=>{});});
  await page.getByRole('button',{name:'本人只读核对原事件',exact:true}).click();await ready;await apply(tokens.bob);release();await page.unrouteAll({behavior:'wait'});
  assert.match(await page.getByRole('status').innerText(),/身份已应用/);
  assert.equal(await page.getByRole('button',{name:'本人只读核对原事件',exact:true}).isDisabled(),false);
 }finally{await context.close();await engine.close();}
});
