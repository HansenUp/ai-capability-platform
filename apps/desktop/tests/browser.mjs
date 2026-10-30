import {createServer} from "node:http";
import {readFile} from "node:fs/promises";
import assert from "node:assert/strict";
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE);
const root=new URL("../isolated/",import.meta.url);
const server=createServer(async(req,res)=>{const file={"/":"index.html","/host.mjs":"host.mjs","/session.mjs":"session.mjs","/native-bridge.mjs":"native-bridge.mjs"}[req.url];if(!file){res.writeHead(404).end();return;}try{res.setHeader("Content-Type",file.endsWith("html")?"text/html; charset=utf-8":"text/javascript; charset=utf-8");res.end(await readFile(new URL(file,root)));}catch{res.writeHead(500).end();}});
let browser;
try{
 await new Promise(resolve=>server.listen(0,"127.0.0.1",resolve));
 browser=await chromium.launch({channel:"chrome",headless:true});
 const page=await browser.newPage();page.setDefaultTimeout(5000);
 const url=`http://127.0.0.1:${server.address().port}/`;
 const errors=[];page.on("pageerror",e=>errors.push(e.message));
 await page.goto(url);
 await page.locator("#start").click();await page.waitForFunction(()=>document.querySelector("#state").textContent==="READY");
 await page.locator("#login").click();await page.locator("#read").click();await page.locator("#result").waitFor({state:"visible"});
 assert.match(await page.locator("#result").textContent(),/alice/);
 await page.locator("#network").click();await page.locator("#read").click();assert.match(await page.locator("#result").textContent(),/alice/);
 await page.locator("#identity").selectOption("bob");assert.equal(await page.locator("#result").textContent(),"");assert.equal(await page.locator("#result").isVisible(),false);
 await page.locator("#login").click();await page.locator("#read").click();assert.equal(await page.locator("#result").isVisible(),false);
 console.log("PASS offline existing cache and identity switch clearing");
 await page.locator("#network").click();await page.locator("#ttl").fill("1");await page.locator("#login").click();await page.locator("#read").click();await page.locator("#result").waitFor({state:"visible"});
 await page.locator("#result").waitFor({state:"hidden"});assert.equal(await page.locator("#result").textContent(),"");assert.match(await page.locator("#identity-status").textContent(),/失效/);
 console.log("PASS natural expiry clears DOM without user interaction");
 await page.locator("#ttl").fill("60");await page.locator("#login").click();await page.locator("#read").click();await page.locator("#result").waitFor({state:"visible"});
 await page.locator("#fault").selectOption("read");await page.locator("#read").click();await page.getByRole("alert").filter({hasText:"读取失败"}).waitFor();assert.equal(await page.locator("#result").isVisible(),false);
 await page.locator("#fault").selectOption("stop");await page.locator("#stop").click();await page.waitForFunction(()=>document.querySelector("#state").textContent==="CLEANUP_UNKNOWN");assert.equal(await page.locator("#start").isDisabled(),true);
 console.log("PASS read error clearing and unknown cleanup blocks start");
 await page.reload();assert.equal(await page.locator("#result").isVisible(),false);assert.equal(await page.locator("#identity-status").textContent(),"未认证");
 await page.locator("#fault").selectOption("start");await page.locator("#start").click();await page.waitForFunction(()=>document.querySelector("#state").textContent==="FAILED");
 await page.setViewportSize({width:375,height:812});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 await page.locator("#fault").selectOption("none");await page.locator("#start").focus();await page.keyboard.press("Enter");await page.waitForFunction(()=>document.querySelector("#state").textContent==="READY");

 await page.reload();await page.locator("#fault").selectOption("hold-start");await page.locator("#start").click();
 await page.waitForFunction(()=>document.querySelector("#state").textContent==="STARTING");await page.locator("#stop").click();
 await page.waitForFunction(()=>document.querySelector("#state").textContent==="STOPPING");assert.equal(await page.locator("#start").isDisabled(),true);
 await page.locator("#release").click();await page.waitForFunction(()=>document.querySelector("#state").textContent==="STOPPED");
 console.log("PASS interrupted pending startup cleans late handle before restart");
 for(const mode of ["hold-read","hold-read-error"]){
  await page.reload();await page.locator("#start").click();await page.locator("#login").click();
  await page.locator("#fault").selectOption(mode);await page.locator("#read").click();await page.getByText("替身操作暂挂",{exact:true}).waitFor();
  await page.locator("#identity").selectOption("bob");await page.locator("#login").click();
  await page.locator("#fault").selectOption("none");await page.locator("#read").click();await page.locator("#result").waitFor({state:"visible"});
  assert.match(await page.locator("#result").textContent(),/bob/);await page.locator("#release").click();
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  assert.match(await page.locator("#result").textContent(),/bob/);assert.equal(await page.locator("#error").textContent(),"");
 }
 console.log("PASS slow success/error cannot overwrite switched identity content");

 await page.reload();await page.locator("#start").click();await page.locator("#login").click();await page.locator("#read").click();await page.locator("#result").waitFor({state:"visible"});
 await page.locator("#fault").selectOption("stop");await page.locator("#close-intent").click();
 await page.getByText("关闭待核对：CLEANUP_UNKNOWN",{exact:true}).waitFor();assert.equal(await page.locator("#result").textContent(),"");assert.equal(await page.locator("#start").isDisabled(),true);
 for(const proof of ["absent","foreign","alive"]){await page.locator("#proof").selectOption(proof);await page.locator("#reconcile").click();assert.equal(await page.locator("#close-state").textContent(),"关闭待核对：CLEANUP_UNKNOWN");}
 await page.locator("#proof").selectOption("matched");await page.locator("#reconcile").click();await page.getByText("退出证据匹配：允许关闭（仅模拟）",{exact:true}).waitFor();
 assert.equal(await page.locator("#calls").textContent(),"停止调用：1 · 核对调用：4");assert.equal(await page.locator("#start").isDisabled(),true);
 console.log("PASS close clears cache, blocks restart, exact proof only and no repeated stop");

 await page.reload();await page.locator("#fault").selectOption("hold-start");await page.locator("#start").click();await page.getByText("替身操作暂挂",{exact:true}).waitFor();
 await page.locator("#close-intent").click();assert.equal(await page.locator("#start").isDisabled(),true);assert.equal(await page.locator("#login").isDisabled(),true);
 await page.locator("#release").click();await page.getByText("退出证据匹配：允许关闭（仅模拟）",{exact:true}).waitFor();
 assert.equal(await page.locator("#calls").textContent(),"停止调用：1 · 核对调用：0");assert.equal(await page.locator("#state").textContent(),"STOPPED");
 console.log("PASS close during pending start waits for owned handle then exits once");
 assert.deepEqual(errors,[]);console.log("PASS reload isolation, failed start, narrow viewport and keyboard start");
}finally{await browser?.close();await new Promise(resolve=>server.close(resolve));console.log("CLEANUP owned browser and loopback static server stopped");}
