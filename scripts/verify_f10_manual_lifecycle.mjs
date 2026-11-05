/** F10真实生命周期验收：真实时钟到期、手动停止、启动中断和浏览器验证失败。 */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdtemp,mkdir,readFile,readdir,rm,writeFile,access} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {parseArgs} from 'node:util';
import {setTimeout as sleep} from 'node:timers/promises';
import {preflight} from './demo-preflight.mjs';
import {stopOwnedChild} from './demo-cleanup.mjs';
import {ownedBrowser} from '../apps/console-web/tests/browser/owned-browser.mjs';
const root=resolve(fileURLToPath(new URL('..',import.meta.url)));
const {values}=parseArgs({options:{'report-dir':{type:'string'},case:{type:'string',default:'expiry'}},strict:true});
if(!values['report-dir']||!['expiry','stop','interrupt','browser-failure'].includes(values.case))throw new Error('--report-dir and valid --case required');
const evidence=resolve(values['report-dir']);await mkdir(evidence,{recursive:true});
const config=await preflight({root,args:['--f10-readonly'],env:process.env});
const temporary=await mkdtemp(join(tmpdir(),'f10-manual-lifecycle-'));
let child,browser,context,ready,log='',interrupted=false,metadata,owned,spawnError;
const report={case:values.case,status:'RUNNING',real_wall_clock:true,started_at:new Date().toISOString()};
let resolveExit;const ended=new Promise(r=>{resolveExit=r;});
async function absent(path){await assert.rejects(access(path),{code:'ENOENT'});}
async function refused(url){await assert.rejects(fetch(url,{signal:AbortSignal.timeout(2000)}));}
try{
 let playwright=config.playwright;
 if(values.case==='browser-failure'){
  playwright=join(temporary,'failure.mjs');await writeFile(playwright,"export const chromium={async launchServer(){throw new Error('SYNTHETIC_BROWSER_FAILURE')}};\n");
 }
 child=spawn(process.execPath,['scripts/start_local_demo.mjs','--f10-readonly','--lifetime-seconds=60'],{cwd:root,env:{PATH:process.env.PATH,HOME:process.env.HOME,TMPDIR:temporary,LANG:'en_US.UTF-8',PLAYWRIGHT_MODULE:playwright,S12_DEMO_EVIDENCE:join(evidence,'startup')},stdio:['ignore','pipe','pipe']});
 child.once('exit',(code,signal)=>resolveExit({code,signal}));child.once('error',e=>{spawnError=e;resolveExit({error:e.code});});
 const collect=bytes=>{
  log+=bytes.toString();
  if(values.case==='interrupt'&&!interrupted&&log.includes('DEMO_PHASE: fixture-ready')){interrupted=true;child.kill('SIGINT');}
  const match=log.match(/DEMO_READY (http:\/\/127\.0\.0\.1:\d+\/demo) expires=(\S+) pid=(\d+) portal=(\S+)/);
  if(match&&!ready){assert.equal(Number(match[3]),child.pid);ready={url:match[1],expiry:Date.parse(match[2]),portal:match[4]};}
 };
 child.stdout.on('data',collect);child.stderr.on('data',collect);
 const startupDeadline=Date.now()+45000;
 while(!ready&&child.exitCode===null&&child.signalCode===null){
  if(spawnError)throw spawnError;if(Date.now()>=startupDeadline)throw new Error('Lifecycle launcher readiness exceeded bound');await sleep(100);
 }
 if(['interrupt','browser-failure'].includes(values.case)){
  const result=await ended;assert.equal(result.code,1);assert.equal(ready,undefined);assert.match(log,/CLEANUP: owned demo temporary root removed/);assert.match(log,/ephemeral test token file removed/);
  if(values.case==='browser-failure')assert.match(log,/SYNTHETIC_BROWSER_FAILURE/);else assert.equal(interrupted,true);
  assert.deepEqual((await readdir(temporary)).filter(n=>n.startsWith('ai-platform-demo-')),[]);report.expected_startup_failure=true;
 }else{
  assert.ok(ready,'Readiness must follow actual browser acceptance');assert.ok(ready.expiry>Date.now()&&ready.expiry-Date.now()<=60000);
  owned=(await readdir(temporary)).filter(n=>n.startsWith('ai-platform-demo-'));assert.equal(owned.length,1);const demoRoot=join(temporary,owned[0]);metadata=JSON.parse(await readFile(join(demoRoot,'fixture.json'),'utf8'));
  assert.ok(metadata.tokens.startsWith(demoRoot+'/'));for(const endpoint of [ready.url,ready.portal,metadata.runtime,metadata.console])assert.equal(new URL(endpoint).hostname,'127.0.0.1');
  const info=await(await fetch(new URL('/__demo/info',ready.url))).json();assert.equal(info.profile,'f10-readonly-v1');assert.equal(Date.parse(info.expires_at),ready.expiry);
  report.expires_at=info.expires_at;
  const pw=await import(config.playwright);browser=await ownedBrowser(pw.chromium,{channel:'chrome',headless:true});context=await browser.newContext();const page=await context.newPage(),consolePage=await context.newPage();page.setDefaultTimeout(5000);consolePage.setDefaultTimeout(5000);
  await consolePage.goto(new URL('/manage/queries',ready.url).href);await consolePage.getByLabel('演示测试主体',{exact:true}).selectOption('alice');
  const posts=[];page.on('request',r=>{if(r.method()==='POST'&&new URL(r.url()).pathname.startsWith('/v1/'))posts.push(new URL(r.url()).pathname);});
  await page.goto(ready.portal+'#/queries');await page.getByRole('button',{name:'预览门户查询',exact:true}).click();await page.getByRole('button',{name:'确认范围并打开门户查询工作台',exact:true}).click();await page.getByRole('button',{name:'应用门户登录身份并核对账本',exact:true}).click();await page.getByRole('status').filter({hasText:/验签身份与账本读取完成/}).waitFor();await page.getByRole('button',{name:'填入订单统计草稿（不创建）',exact:true}).click();
  const registered=page.waitForResponse(r=>r.url().endsWith('/v1/workbench/create-attempts')&&r.request().method()==='POST');await page.getByRole('button',{name:'登记新 Run（尚未创建）',exact:true}).click();assert.equal((await(await registered).json()).status,'PREPARED');
  assert.deepEqual(posts,['/v1/workbench/create-attempts']);
  if(values.case==='stop')child.kill('SIGINT');else while(Date.now()<ready.expiry){assert.equal(child.exitCode,null);await sleep(Math.min(500,ready.expiry-Date.now()));}
  let timer;const result=await Promise.race([ended,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('Launcher did not stop and clean by expiry bound')),15000);})]).finally(()=>clearTimeout(timer));assert.deepEqual(result,{code:0,signal:null});
  if(values.case==='expiry'){
   await page.getByRole('alert').filter({hasText:'演示身份已到期'}).waitFor();assert.equal(await page.getByRole('region',{name:'门户查询执行',exact:true}).count(),0);await consolePage.getByText('演示身份已到期，请停止或重新启动独立环境。',{exact:true}).waitFor();assert.equal(await consolePage.getByLabel('演示测试主体',{exact:true}).isDisabled(),true);
   assert.match(log,/DEMO_EXPIRED/);
  }
  assert.deepEqual(posts,['/v1/workbench/create-attempts']);for(const url of [ready.url,ready.portal,metadata.runtime+'/healthz',metadata.console+'/healthz'])await refused(url);
  await absent(metadata.tokens);await absent(demoRoot);assert.match(log,/CLEANUP: owned demo temporary root removed/);report.prepared_attempt_not_executed=true;report.no_auto_retry=true;report.all_owned_endpoints_stopped=true;report.tokens_and_temporary_database_removed=true;
 }
 report.status='PASS';
}catch(e){report.status='FAIL';report.error=e.message;process.exitCode=1;}
finally{
 const cleanupErrors=[];for(const resource of [context,browser])try{await resource?.close();}catch(e){cleanupErrors.push(e.message);}
 try{await stopOwnedChild(child);}catch(e){cleanupErrors.push(e.message);}
 if(!cleanupErrors.length)await rm(temporary,{recursive:true,force:true});
 if(cleanupErrors.length){report.status='FAIL';report.cleanup_errors=cleanupErrors;process.exitCode=1;}
 report.finished_at=new Date().toISOString();await writeFile(join(evidence,'launcher.log'),log);await writeFile(join(evidence,'result.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));
}
