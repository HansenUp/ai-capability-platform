import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { browser, profile } from "./support.mjs";
let engine, tokens;
before(async () => { engine = await browser(); tokens = JSON.parse(await readFile(profile.tokens,"utf8")); });
after(async () => { await engine?.close(); });
async function login(page, user="alice") {
  await page.getByLabel("测试用户令牌").fill(tokens["node_"+user]);
  await page.getByRole("button",{name:"应用身份",exact:true}).click();
  await page.getByRole("button",{name:"读取权威状态与未决操作"}).click();
  await page.getByText(`验签身份：default / ${user}`).waitFor();
}
async function run(action) {
 const context=await engine.newContext(); const page=await context.newPage();page.setDefaultTimeout(5000);
 try {await page.goto(profile.url+"/manage/node-control");await login(page);await action(page);}
 finally {await context.close();}
}
test("F7 signed real local API: drain ACK then explicit resume", async()=>run(async page=>{
 const node=page.locator("article").filter({has:page.getByRole("heading",{name:"f7-node-normal",exact:true})});
 await node.getByRole("button",{name:"登记排空",exact:true}).click();
 await page.getByRole("button",{name:"显式执行一次"}).click();
 await node.getByText(/DRAINED/).waitFor();
 assert.equal(await node.getByRole("button",{name:"登记恢复"}).isDisabled(),true);
 await page.getByRole("button",{name:"本人确认收口 ACK"}).click();
 await page.waitForFunction(()=>[...document.querySelectorAll("article")].find(e=>e.querySelector("h3")?.textContent==="f7-node-normal")?.textContent.includes("服务端允许新登记"));
 await node.getByRole("button",{name:"登记恢复"}).click();
 await page.getByRole("button",{name:"显式执行一次"}).click();
 await node.getByText(/SERVING/).waitFor();
 await page.getByRole("button",{name:"本人确认收口 ACK"}).click();
}));
test("F7 signed real local API: reload withdraw and lost effect response recovery", async()=>run(async page=>{
 const node=page.locator("article").filter({has:page.getByRole("heading",{name:"f7-node-reload",exact:true})});
 await node.getByRole("button",{name:"登记排空",exact:true}).click();
 await page.getByRole("button",{name:"显式执行一次"}).waitFor();
 await page.reload();await login(page);
 assert.equal(await page.getByRole("button",{name:"显式执行一次"}).count(),0);
 await page.getByRole("button",{name:"本人撤销登记"}).click();
 await page.getByRole("button",{name:"本人确认收口 ACK"}).click();
 const loss=page.locator("article").filter({has:page.getByRole("heading",{name:"f7-node-loss",exact:true})});
 await loss.getByRole("button",{name:"登记排空",exact:true}).click();
 await page.getByRole("button",{name:"显式执行一次"}).click();
 await page.getByRole("alert").filter({hasText:"HTTP 503"}).waitFor();
 await page.reload();await login(page);
 await page.getByRole("button",{name:"只读对账原结果"}).click();
 await loss.getByText(/DRAINED/).waitFor();
 await page.getByRole("button",{name:"本人确认收口 ACK"}).click();
 await login(page,"bob");
 assert.equal(await loss.getByRole("button",{name:"登记恢复"}).isDisabled(),true);
}));


test("F7 two tabs: recovered preparation cancellation fences stale execute", async()=>run(async page=>{
 const second=await page.context().newPage(); second.setDefaultTimeout(5000);
 const firstNode=page.locator("article").filter({has:page.getByRole("heading",{name:"f7-node-tabs",exact:true})});
 await firstNode.getByRole("button",{name:"登记排空",exact:true}).click();
 await page.getByRole("button",{name:"显式执行一次"}).waitFor();
 await second.goto(profile.url+"/manage/node-control");await login(second);
 assert.equal(await second.getByRole("button",{name:"显式执行一次"}).count(),0);
 await second.getByRole("button",{name:"本人撤销登记"}).click();
 await second.getByRole("button",{name:"本人确认收口 ACK"}).waitFor();
 await page.getByRole("button",{name:"显式执行一次"}).click();
 await page.getByRole("button",{name:"本人确认收口 ACK"}).waitFor();
 await firstNode.getByText(/SERVING/).waitFor();
 await page.getByRole("button",{name:"本人确认收口 ACK"}).click();
 await second.close();
}));


test("F7 timeout ACK retains original receipt navigation for natural drain", async()=>run(async page=>{
 const node=page.locator("article").filter({has:page.getByRole("heading",{name:"f7-node-timeout",exact:true})});
 await node.getByRole("button",{name:"登记排空",exact:true}).click();
 await page.getByRole("button",{name:"显式执行一次"}).click();
 await node.getByText(/DRAINING/).waitFor();
 await page.getByRole("button",{name:"本人确认收口 ACK"}).click();
 await page.reload();await login(page);
 await node.getByRole("button",{name:"读取原排空回执"}).click();
 await page.getByRole("button",{name:"读取自然排空观测"}).click();
 await node.getByText(/DRAINED/).waitFor();
 assert.equal(await node.getByRole("button",{name:"登记恢复"}).isDisabled(),false);
}));

test("F7 signed concurrency configuration: explicit apply reload ACK preserves serving", async()=>run(async page=>{
 const node=page.locator("article").filter({has:page.getByRole("heading",{name:"f7-node-limit",exact:true})});
 await node.getByLabel("新并发上限").fill("2");
 await node.getByRole("button",{name:"登记并发上限",exact:true}).click();
 await page.getByText(/configure_limit.*PREPARED/).waitFor();
 await page.getByRole("button",{name:"显式执行一次"}).click();
 await node.getByText(/HTTP／SSE并发上限：2/).waitFor();
 await node.getByText(/SERVING/).waitFor();
 assert.equal(await node.getByRole("button",{name:"登记排空",exact:true}).isDisabled(),true);
 await page.reload();await login(page);
 await node.getByText(/HTTP／SSE并发上限：2/).waitFor();
 await page.getByRole("button",{name:"本人确认收口 ACK"}).click();
 await page.waitForFunction(()=>[...document.querySelectorAll("article")].find(e=>e.querySelector("h3")?.textContent==="f7-node-limit")?.textContent.includes("服务端允许新登记"));
 await node.getByLabel("新并发上限").fill("1");
 await node.getByRole("button",{name:"登记并发上限",exact:true}).click();
 await page.getByRole("button",{name:"显式执行一次"}).waitFor();
 await page.reload();await login(page);
 assert.equal(await page.getByRole("button",{name:"显式执行一次"}).count(),0);
 await page.getByRole("button",{name:"本人撤销登记"}).click();
 await page.getByRole("button",{name:"本人确认收口 ACK"}).click();
 await node.getByText(/HTTP／SSE并发上限：2/).waitFor();
}));
