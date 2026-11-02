// Browser contract tests with a native-command fixture; not a WebView/ACL verdict.
import {createServer} from 'node:http';
import {readFile,writeFile,mkdir} from 'node:fs/promises';
import assert from 'node:assert/strict';
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE);
const root=new URL('../native-candidate/ui/',import.meta.url);
const evidence=new URL('../native-candidate/g2-b-preflight/',import.meta.url);
const results=[];
const server=createServer(async(req,res)=>{
 const file={'/':'index.html','/entry.mjs':'entry.mjs','/host.mjs':'host.mjs','/session.mjs':'session.mjs','/style.css':'style.css'}[req.url];
 if(!file){res.writeHead(404).end();return;}
 try{res.setHeader('Content-Type',file.endsWith('html')?'text/html; charset=utf-8':file.endsWith('css')?'text/css':'text/javascript; charset=utf-8');res.end(await readFile(new URL(file,root)));}catch{res.writeHead(500).end();}
});
let browser;
try{
 await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
 browser=await chromium.launch({channel:'chrome',headless:true});
 const page=await browser.newPage();page.setDefaultTimeout(5000);
 await page.addInitScript(()=>{
  window.fixture={state:sessionStorage.getItem('native-fixture-state')??'STOPPED',calls:[],hold:false};
  window.__TAURI_INTERNALS__={invoke:async(command,{message})=>{
   const f=window.fixture;f.calls.push(message.action);
   if(f.hold&&message.action==='status')await new Promise(resolve=>{f.release=resolve;});
   if(message.action==='start')f.state='READY';
   if(message.action==='stop')f.state='STOPPED';
   if(message.action==='close')f.state='CLOSED';
   sessionStorage.setItem('native-fixture-state',f.state);return f.state;
  }};
 });
 await page.goto(`http://127.0.0.1:${server.address().port}/`);
 const click=id=>page.locator('#'+id).click();
 const state=value=>page.waitForFunction(v=>document.querySelector('#state').textContent===v,value);
 const pass=name=>{results.push(name);console.log('PASS '+name);};
 await click('start');await state('READY');await click('login');await click('read');
 await page.locator('#result').waitFor({state:'visible'});assert.match(await page.locator('#result').textContent(),/alice/);
 await click('network');const before=await page.evaluate(()=>fixture.calls.length);await click('read');
 assert.match(await page.locator('#result').textContent(),/alice/);assert.equal(await page.evaluate(()=>fixture.calls.length),before);
 await page.locator('#identity').selectOption('bob');assert.equal(await page.locator('#result').textContent(),'');
 await click('login');await click('read');assert.equal(await page.locator('#result').isVisible(),false);
 pass('offline cache performs no native read; switching identity cannot read previous cache');
 await click('network');await page.locator('#ttl').fill('1');await click('login');await click('read');
 await page.locator('#result').waitFor({state:'visible'});await page.locator('#result').waitFor({state:'hidden'});
 assert.equal(await page.locator('#result').textContent(),'');pass('natural expiry clears visible content');
 await page.locator('#ttl').fill('30');await click('login');await click('read');await click('stop');await state('STOPPED');
 assert.equal(await page.locator('#result').textContent(),'');assert.equal(await page.locator('#read').isDisabled(),true);
 pass('stop removes identity and cache');
 await click('start');await state('READY');await click('login');await click('read');
 await page.reload();await state('READY');assert.equal(await page.locator('#result').isVisible(),false);assert.equal(await page.locator('#read').isDisabled(),true);
 pass('reload discards identity and cache while native process state is recovered');
 await click('login');await page.evaluate(()=>{fixture.hold=true;});await click('read');
 await page.waitForFunction(()=>typeof fixture.release==='function');await click('close');
 assert.equal(await page.locator('#result').textContent(),'');await page.evaluate(()=>{fixture.hold=false;fixture.release();});
 await state('CLOSED');assert.equal(await page.locator('#result').isVisible(),false);assert.equal(await page.locator('#start').isDisabled(),true);
 assert.equal(await page.evaluate(()=>fixture.calls.filter(x=>x==='close').length),1);
 pass('close during pending read is queued once, clears content and prevents restart');
 await page.setViewportSize({width:375,height:812});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 pass('375px viewport has no horizontal overflow');
 await mkdir(evidence,{recursive:true});await writeFile(new URL('native-ui-result.json',evidence),JSON.stringify({status:'PASS',scope:'browser-native-command-fixture',cases:results},null,2));
}finally{await browser?.close();await new Promise(resolve=>server.close(resolve));console.log('CLEANUP browser and loopback server');}
