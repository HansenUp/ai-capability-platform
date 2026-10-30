import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import { browser, profile } from "./support.mjs";
let engine;
before(async () => { engine = await browser(); });
after(async () => { await engine?.close(); });
async function fixture(action) {
  const context = await engine.newContext(); const page = await context.newPage(); page.setDefaultTimeout(5000);
  const identity = JSON.stringify(["https://test.invalid", "test", "alice"]);
  const node = { id:"synthetic",generation:0,state:"SERVING",desired:"OPEN",control_busy:false,can_resume:false };
  let op; let lost = false; const writes = [];
  await page.route("**/console/**", async route => {
    const req = route.request(), path = new URL(req.url()).pathname.replace("/console/isolated-node-control", "");
    if (!req.url().includes("isolated-node-control")) return route.fulfill({status:503,json:{}});
    if (req.method() === "GET") {
      const json = path === "" ? {synthetic:true,issuer:"https://test.invalid",tenant_id:"test",subject:"alice",nodes:[node]} :
        path === "/operations" ? {synthetic:true,operations:op && !op.acknowledged ? [op] : []} : op;
      return route.fulfill({json});
    }
    writes.push(path); const body = req.postDataJSON();
    if (path === "/prepare") { op = {...body,id:body.attempt,actor:identity,generation:1,version:1,status:"PREPARED",acknowledged:false,evidence:null}; node.control_busy=true; }
    else if (path.endsWith("/execute")) { op.status="SUCCEEDED";op.evidence={outcome:"drained"}; node.state="DRAINED"; node.desired="CLOSED"; if (lost) return route.abort("failed"); }
    else if (path.endsWith("/withdraw")) { op.status="CANCELLED"; op.evidence={outcome:"cancelled"}; }
    else if (path.endsWith("/ack")) { assert.deepEqual(body.receipt,op);op.acknowledged=true;node.control_busy=false; }
    return route.fulfill({json:op});
  });
  async function login() {
    await page.getByLabel("测试用户令牌").fill("synthetic-ui-token"); await page.getByRole("button",{name:"应用身份",exact:true}).click();
    await page.getByRole("button",{name:"读取权威状态与未决操作"}).click();
    await page.getByText("验签身份：test / alice").waitFor();
  }
  try { await page.goto(profile.url+"/manage/node-control");await login(); await action({page,writes,login,lose:()=>{lost=true;}}); }
  finally { await context.close(); }
}
test("F7 page: reload preparation only withdraws; ACK separately releases control", async () => fixture(async ({page,writes,login}) => {
  await page.getByRole("button",{name:"登记排空",exact:true}).click();
  await page.getByRole("button",{name:"显式执行一次"}).waitFor();
  await page.reload();await login();
  assert.equal(await page.getByRole("button",{name:"显式执行一次"}).count(),0);
  await page.getByRole("button",{name:"本人撤销登记"}).click();
  await page.getByRole("button",{name:"本人确认收口 ACK"}).waitFor();
  assert.equal(await page.getByRole("button",{name:"登记排空",exact:true}).isDisabled(),true);
  await page.getByRole("button",{name:"本人确认收口 ACK"}).click();
  await page.getByRole("button",{name:"登记排空",exact:true}).waitFor();
  await page.waitForFunction(()=>[...document.querySelectorAll("button")].some(b=>b.textContent==="登记排空"&&!b.disabled));
  assert.equal(writes.filter(p=>p.endsWith("/execute")).length,0);
}));
test("F7 page: lost response reads original result, ACK never restores admission", async () => fixture(async ({page,writes,lose}) => {
  lose(); await page.getByRole("button",{name:"登记排空",exact:true}).click();
  await page.getByRole("button",{name:"显式执行一次"}).click();
  await page.getByRole("alert").waitFor();
  await page.getByRole("button",{name:"读取权威状态与未决操作"}).click();
  await page.getByRole("button",{name:"本人确认收口 ACK"}).click();
  await page.getByText(/DRAINED/).waitFor();
  assert.equal(writes.filter(p=>p.endsWith("/execute")).length,1);
  assert.equal(writes.filter(p=>p.endsWith("/ack")).length,1);
  await page.getByLabel("测试用户令牌").fill("different");await page.getByRole("button",{name:"应用身份",exact:true}).click();
  assert.equal(await page.getByText(/DRAINED/).count(),0);
}));


for (const mode of ["identity", "unmount"]) test(`F7 old snapshot is isolated after ${mode}`, async()=>fixture(async ({page,writes})=>{
 let release, started;
 const held=new Promise(resolve=>{release=resolve;}); const seen=new Promise(resolve=>{started=resolve;});
 await page.route("**/console/isolated-node-control",async route=>{
   started();await held;
   await route.fulfill({json:{synthetic:true,issuer:"https://test.invalid",tenant_id:"test",subject:"alice",nodes:[{id:"old-node",generation:1,state:"SERVING",desired:"OPEN",control_busy:false,can_resume:false}]}});
 });
 await page.getByRole("button",{name:"读取权威状态与未决操作"}).click();await seen;
 if(mode==="identity") {
   await page.getByLabel("测试用户令牌").fill("other");await page.getByRole("button",{name:"应用身份",exact:true}).click();
 } else {
   await page.getByRole("link",{name:"返回节点历史与诊断"}).click();
   await page.getByRole("heading",{name:"节点分组与路由",exact:true}).waitFor();
 }
 const response=page.waitForResponse(r=>r.url().endsWith("/isolated-node-control"));release();await (await response).finished();
 await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>resolve())));
 assert.equal(await page.getByText("old-node",{exact:true}).count(),0);
 assert.equal(await page.getByText("验签身份：test / alice",{exact:true}).count(),0);
 assert.equal(writes.length,0);
}));
