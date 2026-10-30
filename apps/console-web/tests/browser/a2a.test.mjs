import { test } from "node:test";
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import { browser, profile } from "./support.mjs";

const root = "/v1/isolated-a2a";
const extension = "urn:ai-platform:isolated:workbench:1";
const evidence = value => Object.fromEntries(["attempt_id", "revision", "fingerprint"].map(key => [key, value.receipt[key]]));
async function call(page, token, path, body) {
  return page.evaluate(async ({ token, path, body, extension }) => {
    try {
      const response = await fetch(path, { method: body ? "POST" : "GET", cache: "no-store", redirect: "error", credentials: "omit",
        headers: { Authorization: `Bearer ${token}`, "A2A-Version": "1.0", "A2A-Extensions": extension, "Content-Type": "application/json" },
        ...(body ? { body: JSON.stringify(body) } : {}) });
      return { status: response.status, value: await response.json() };
    } catch { return { unknown: true }; }
  }, { token, path, body, extension });
}

test("E2 actual HTTP: lost responses, reload, two tabs, owner recovery and explicit ACK", async () => {
  const tokens = JSON.parse(await readFile(profile.tokens, "utf8"));
  const engine = await browser(), context = await engine.newContext(), page = await context.newPage();
  try {
    await page.goto(profile.url);
    const catalog = await call(page, tokens.alice, "/v1/isolated-discovery/agents");
    assert.equal(catalog.status, 200);
    assert.deepEqual(catalog.value.items, [{ agent_id: "s12-workbench", version: "1" }]);
    const card = await call(page, tokens.alice, root + "/.well-known/agent-card.json");
    assert.equal(card.status, 200);
    assert.ok(card.value.capabilities.extensions.some(value => value.uri === extension && value.required));
    const body = { attempt_id: randomUUID(), agent_id: catalog.value.items[0].agent_id,
      message: { messageId: randomUUID(), role: "ROLE_USER", parts: [{ text: "CREATE-DURABLE-SYNTHETIC" }] },
      configuration: { returnImmediately: true } };
    let prepared;
    await page.route(`**${root}/prepare`, async route => {
      const response = await route.fetch(); assert.equal(response.status(), 200); prepared = await response.json(); await route.abort();
    });
    assert.equal((await call(page, tokens.alice, root + "/prepare", body)).unknown, true);
    assert.equal(prepared.taskId, null);
    await page.unroute(`**${root}/prepare`);
    await page.reload();
    // Reload may lose the host's in-memory attempt ID. Discover the server-side active lock first.
    const active = await call(page, tokens.alice_fresh, "/v1/workbench/attempts");
    assert.equal(active.status, 200);
    assert.equal(active.value.active.action, "a2a_create");
    assert.equal(active.value.active.attempt_id, body.attempt_id);
    const path = root + "/attempts/" + active.value.active.attempt_id;
    assert.deepEqual((await call(page, tokens.alice_fresh, path)).value, prepared);
    for (const token of [tokens.bob, tokens.foreign]) assert.equal((await call(page, token, path)).status, 404);
    await context.setOffline(true);
    assert.equal((await call(page, tokens.alice_fresh, path)).unknown, true);
    await context.setOffline(false);
    const second = await context.newPage(); await second.goto(profile.url);
    let committed;
    // Both tabs dispatch explicitly; server grants only one execution. Drop its response.
    await context.route(`**${root}/message:send`, async route => {
      const response = await route.fetch();
      if (response.status() === 200) { committed = (await response.json()).task.metadata[extension]; await route.abort(); }
      else await route.fulfill({ response });
    });
    const send = { message: body.message, configuration: body.configuration, metadata: { [extension]: evidence(prepared) } };
    const results = await Promise.all([call(page, tokens.alice_fresh, root + "/message:send", send), call(second, tokens.alice_fresh, root + "/message:send", send)]);
    assert.equal(results.filter(r => r.unknown).length, 1); assert.equal(results.filter(r => r.status === 409).length, 1);
    assert.equal(committed.receipt.status, "COMMITTED");
    await page.reload();
    const recovered = (await call(page, tokens.alice_fresh, path)).value;
    assert.equal(recovered.receipt.acknowledged, false);
    const task = await call(page, tokens.alice_fresh, root + "/tasks/" + recovered.taskId);
    assert.equal(task.status, 200); assert.equal(task.value.id, recovered.taskId);
    assert.equal((await call(page, tokens.bob, root + "/tasks/" + recovered.taskId)).status, 404);
    await page.waitForFunction(async ({ path, token, extension }) => {
      const response = await fetch(path, { cache: "no-store", headers: { Authorization: `Bearer ${token}`,
        "A2A-Version": "1.0", "A2A-Extensions": extension } });
      return response.ok && (await response.json()).status.state === "TASK_STATE_COMPLETED";
    }, { path: root + "/tasks/" + recovered.taskId, token: tokens.alice_fresh, extension }, { timeout: 10000 });
    const completed = (await call(page, tokens.alice_fresh, root + "/tasks/" + recovered.taskId)).value;
    assert.equal(completed.artifacts.length, 1);
    assert.equal(completed.artifacts[0].metadata[extension].projection, "final-assistant-text-v1");
    const reader = await context.newPage(); await reader.goto(profile.url + "/a2a-task");
    const readonlyWrites = [];
    reader.on("request", request => { if (request.method() !== "GET" && request.url().includes("/v1/")) readonlyWrites.push(request.url()); });
    await reader.getByLabel("用户访问令牌", { exact: true }).fill(tokens.alice_fresh);
    await reader.getByRole("button", { name: "应用身份并清空旧结果", exact: true }).click();
    await reader.getByLabel("Task ID", { exact: true }).fill(recovered.taskId);
    await reader.getByRole("button", { name: "只读查询 Task", exact: true }).click();
    await reader.locator("pre.task-result").waitFor();
    assert.equal(await reader.locator("pre.task-result").innerText(), completed.artifacts[0].parts[0].text);
    // Hold an old identity's GET response, switch identity, then release it.
    let release, observed;
    const seen = new Promise(resolve => { observed = resolve; });
    const hold = new Promise(resolve => { release = resolve; });
    await reader.route(`**${root}/tasks/*`, async route => {
      const response = await route.fetch(); observed(); await hold;
      await route.fulfill({ response }).catch(() => {});
    });
    await reader.getByRole("button", { name: "只读查询 Task", exact: true }).click();
    await seen;
    await reader.getByLabel("用户访问令牌", { exact: true }).fill(tokens.bob);
    await reader.getByRole("button", { name: "应用身份并清空旧结果", exact: true }).click();
    release(); await reader.unrouteAll({ behavior: "wait" });
    assert.equal(await reader.locator("pre.task-result").count(), 0);
    await reader.getByRole("button", { name: "只读查询 Task", exact: true }).click();
    await reader.getByRole("status").filter({ hasText: "HTTP 404" }).waitFor();
    assert.equal(await reader.locator("pre.task-result").count(), 0);
    assert.deepEqual(readonlyWrites, []);
    await reader.close();
    const events = (await call(page, tokens.alice_fresh, "/v1/runs/" + recovered.taskId + "/events")).value;
    assert.equal(events.filter(event => event.type === "run.created").length, 1);
    assert.equal((await call(page, tokens.alice_fresh, root + "/prepare", { ...body, attempt_id: randomUUID(), message: { ...body.message, messageId: randomUUID() } })).status, 409);
    await page.route(`**${root}/acknowledge`, async route => {
      const response = await route.fetch(); assert.equal(response.status(), 200); await route.abort();
    });
    assert.equal((await call(page, tokens.alice_fresh, root + "/acknowledge", evidence(recovered))).unknown, true);
    assert.equal((await call(page, tokens.alice_fresh, path)).value.receipt.acknowledged, true);
    assert.equal((await call(page, tokens.alice_fresh, "/v1/workbench/attempts")).value.active, null);
    assert.deepEqual((await call(page, tokens.alice_fresh, root + "/tasks/" + recovered.taskId)).value, completed);
    assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
  } finally { await context.close(); await engine.close(); }
});


test("E2 readonly renderer: text is inert, changed task and missing evidence clear old result", async () => {
  const engine = await browser(), context = await engine.newContext(), page = await context.newPage();
  const text = '<img src="https://invalid.example/x" onerror="window.UNSAFE=1"><script>window.UNSAFE=1</script>';
  const writes = [];
  page.on("request", request => { if (request.url().includes("/v1/") && request.method() !== "GET") writes.push(request.method()); });
  try {
    await page.route(`**${root}/tasks/*`, route => {
      const id = decodeURIComponent(new URL(route.request().url()).pathname.split("/").at(-1));
      return route.fulfill({ json: { id, contextId: "synthetic", status: { state: "TASK_STATE_COMPLETED" },
        ...(id === "synthetic-result" ? { artifacts: [{ artifactId: "synthetic-final", parts: [{ text }],
          metadata: { [extension]: { projection: "final-assistant-text-v1" } } }] } : {}) } });
    });
    await page.goto(profile.url + "/a2a-task");
    await page.getByLabel("用户访问令牌", { exact: true }).fill("synthetic-invalid-token-for-http-substitute-only");
    await page.getByRole("button", { name: "应用身份并清空旧结果", exact: true }).click();
    await page.getByLabel("Task ID", { exact: true }).fill("synthetic-result");
    await page.getByRole("button", { name: "只读查询 Task", exact: true }).click();
    await page.locator("pre.task-result").waitFor();
    assert.equal(await page.locator("pre.task-result").innerText(), text);
    assert.equal(await page.locator("pre.task-result img, pre.task-result script").count(), 0);
    assert.equal(await page.evaluate(() => window.UNSAFE), undefined);
    await page.getByLabel("Task ID", { exact: true }).fill("missing-evidence");
    assert.equal(await page.locator("pre.task-result").count(), 0);
    await page.getByRole("button", { name: "只读查询 Task", exact: true }).click();
    await page.getByText("暂无可核验的最终文本；不回溯中间回答，不把缺失当作空结果。").waitFor();
    assert.deepEqual(writes, []);
  } finally { await context.close(); await engine.close(); }
});


test("E2 graphical host: explicit writes, lost response recovery, identity isolation and owner ACK", async () => {
  const tokens = JSON.parse(await readFile(profile.tokens, "utf8"));
  const engine = await browser(), context = await engine.newContext(), page = await context.newPage();
  page.setDefaultTimeout(5000);
  const posts = [];
  page.on("request", request => { if (request.method() === "POST" && request.url().includes("/v1/")) posts.push(new URL(request.url()).pathname); });
  async function identity(token) {
    await page.getByLabel("用户访问令牌", {exact:true}).fill(token);
    await page.getByRole("button", {name:"应用身份并清空旧数据",exact:true}).click();
    await page.getByRole("button", {name:"只读核对身份与回执",exact:true}).click();
    await page.waitForFunction(() => /已核对服务端身份|只读核对失败/.test(document.querySelector('[role="status"]')?.textContent || ''), null, {timeout:5000});
    assert.match(await page.getByRole("status").innerText(), /已核对服务端身份/);
  }
  try {
    await page.goto(profile.url + "/a2a-host"); await identity(tokens.alice_fresh);
    await page.getByLabel("可见 Agent",{exact:true}).selectOption("s12-workbench");
    await page.getByLabel("消息",{exact:true}).fill("CREATE-DURABLE-SYNTHETIC");
    let prepared;
    await page.route(`**${root}/prepare`, async route => { const response=await route.fetch(); assert.equal(response.status(),200); prepared=await response.json(); await route.abort(); });
    await page.getByRole("button",{name:"登记新任务（尚未执行）",exact:true}).click();
    await page.getByRole("status").filter({hasText:"写结果未知"}).waitFor();
    assert.equal(posts.length,1);
    await page.getByRole("button",{name:"只读核对身份与回执",exact:true}).click();
    await page.waitForFunction(() => /已核对服务端身份|只读核对失败/.test(document.querySelector('[role="status"]')?.textContent || ''), null, {timeout:5000});
    assert.match(await page.getByRole("status").innerText(), /已核对服务端身份/);
    assert.equal(posts.length,1);
    const recoveredPage = await context.newPage(); await recoveredPage.goto(profile.url + "/a2a-host");
    await recoveredPage.getByLabel("用户访问令牌",{exact:true}).fill(tokens.alice_fresh);
    await recoveredPage.getByRole("button",{name:"应用身份并清空旧数据",exact:true}).click();
    await recoveredPage.getByRole("button",{name:"只读核对身份与回执",exact:true}).click();
    await recoveredPage.getByText("回执状态：PREPARED",{exact:false}).waitFor();
    assert.equal(await recoveredPage.getByRole("button",{name:"确认执行已登记任务一次",exact:true}).count(),0);
    assert.equal(await recoveredPage.getByRole("button",{name:"登记新任务（尚未执行）",exact:true}).isDisabled(),true);
    await recoveredPage.close();
    let committed;
    await page.route(`**${root}/message:send`, async route => {const response=await route.fetch();assert.equal(response.status(),200);committed=(await response.json()).task.metadata[extension];await route.abort();});
    await page.getByRole("button",{name:"确认执行已登记任务一次",exact:true}).click();
    await page.getByRole("status").filter({hasText:"写结果未知"}).waitFor();
    assert.equal(await page.getByRole("button",{name:"确认执行已登记任务一次",exact:true}).count(),0);
    await page.reload(); await identity(tokens.bob);
    assert.equal(await page.getByRole("region",{name:"A2A回执"}).count(),0);
    await identity(tokens.alice_fresh);
    await page.getByText("回执状态：COMMITTED",{exact:false}).waitFor();
    assert.equal(posts.length,2);
    assert.equal(committed.receipt.attempt_id,prepared.receipt.attempt_id);
    await page.getByRole("button",{name:"预览本人ACK",exact:true}).click();
    await page.getByRole("button",{name:"取消ACK",exact:true}).click();
    assert.equal(posts.length,2);
    await page.route(`**${root}/acknowledge`, async route => {const response=await route.fetch();assert.equal(response.status(),200);await route.abort();});
    await page.getByRole("button",{name:"预览本人ACK",exact:true}).click();
    await page.getByRole("button",{name:"确认本人ACK",exact:true}).click();
    await page.getByRole("status").filter({hasText:"写结果未知"}).waitFor();
    await page.getByRole("button",{name:"只读核对身份与回执",exact:true}).click();
    await page.getByText("本人已确认：是",{exact:true}).waitFor();
    assert.deepEqual(posts,[root+"/prepare",root+"/message:send",root+"/acknowledge"]);
    assert.deepEqual(await page.evaluate(()=>[localStorage.length,sessionStorage.length]),[0,0]);
  } finally {await context.close();await engine.close();}
});

test("E2 withdrawal: reload, unavailable directory, lost terminal and independent ACK", async () => {
 const tokens=JSON.parse(await readFile(profile.tokens,"utf8"));
 const engine=await browser(),context=await engine.newContext(),page=await context.newPage();
 const posts=[]; page.on("request",r=>{if(r.method()==="POST"&&r.url().includes("/v1/"))posts.push(new URL(r.url()).pathname);});
 async function recover(token){
  await page.getByLabel("用户访问令牌",{exact:true}).fill(token);
  await page.getByRole("button",{name:"应用身份并清空旧数据",exact:true}).click();
  await page.getByRole("button",{name:"只读核对身份与回执",exact:true}).click();
  await page.getByRole("status").filter({hasText:"已核对服务端身份"}).waitFor();
 }
 try {
  await page.goto(profile.url+"/a2a-host");await recover(tokens.alice_fresh);
  await page.getByLabel("可见 Agent",{exact:true}).selectOption("s12-workbench");
  await page.getByLabel("消息",{exact:true}).fill("synthetic withdrawal only");
  await page.getByRole("button",{name:"登记新任务（尚未执行）",exact:true}).click();
  await page.getByText("回执状态：PREPARED",{exact:false}).waitFor();
  const active=(await call(page,tokens.alice_fresh,"/v1/workbench/attempts")).value.active;
  await page.reload();
  await page.route("**/v1/isolated-discovery/agents",r=>r.fulfill({status:503,json:{detail:"synthetic unavailable"}}));
  await recover(tokens.bob);assert.equal(await page.getByRole("region",{name:"A2A回执"}).count(),0);
  await recover(tokens.alice_fresh);
  await page.getByRole("status").filter({hasText:"目录不可用"}).waitFor();
  assert.equal(await page.getByRole("button",{name:"确认执行已登记任务一次",exact:true}).count(),0);
  await page.getByRole("button",{name:"预览撤销登记",exact:true}).click();
  await page.getByRole("button",{name:"取消撤销登记",exact:true}).click();assert.equal(posts.length,1);
  await page.route(`**${root}/attempts/*/withdraw`,async r=>{const response=await r.fetch();assert.equal(response.status(),200);await r.abort();});
  await page.getByRole("button",{name:"预览撤销登记",exact:true}).click();
  await page.getByRole("button",{name:"确认本人撤销登记",exact:true}).click();
  await page.getByRole("status").filter({hasText:"写结果未知"}).waitFor();
  assert.equal(await page.getByRole("button",{name:"预览撤销登记",exact:true}).isDisabled(),true);
  await page.reload();await recover(tokens.alice_fresh);
  await page.getByText("终结原因：OWNER_WITHDRAWN",{exact:true}).waitFor();
  await page.getByText("主体新写：锁定",{exact:true}).waitFor();
  await page.getByText("本人已确认：否",{exact:true}).waitFor();
  await page.route(`**${root}/acknowledge`,async r=>{const response=await r.fetch();assert.equal(response.status(),200);await r.abort();});
  await page.getByRole("button",{name:"预览本人ACK",exact:true}).click();
  await page.getByRole("button",{name:"确认本人ACK",exact:true}).click();
  await page.getByRole("status").filter({hasText:"写结果未知"}).waitFor();
  await page.reload();await recover(tokens.alice_fresh);
  await page.getByLabel("恢复 attempt ID（可选）",{exact:true}).fill(active.attempt_id);
  await page.getByRole("button",{name:"只读核对身份与回执",exact:true}).click();
  await page.getByText("本人已确认：是",{exact:true}).waitFor();
  assert.deepEqual(posts,[root+"/prepare",root+"/attempts/"+active.attempt_id+"/withdraw",root+"/acknowledge"]);
  assert.deepEqual(await page.evaluate(()=>[localStorage.length,sessionStorage.length]),[0,0]);
 } finally {await context.close();await engine.close();}
});

test("E2 unified journey: catalog to result and owner ACK, safe error guidance", async()=>{
 const tokens=JSON.parse(await readFile(profile.tokens,"utf8"));
 const engine=await browser(),context=await engine.newContext(),page=await context.newPage();
 try {
  await page.goto(profile.url+"/a2a-host");
  async function applyHost(){
   await page.getByLabel("用户访问令牌",{exact:true}).fill(tokens.alice_fresh);
   await page.getByRole("button",{name:"应用身份并清空旧数据",exact:true}).click();
   await page.getByRole("button",{name:"只读核对身份与回执",exact:true}).click();
   await page.getByRole("status").filter({hasText:"已核对服务端身份"}).waitFor();
  }
  await applyHost();
  await page.getByLabel("可见 Agent",{exact:true}).selectOption("s12-workbench");
  await page.getByLabel("消息",{exact:true}).fill("CREATE-DURABLE-SYNTHETIC");
  await page.getByRole("button",{name:"登记新任务（尚未执行）",exact:true}).click();
  await page.getByText("回执状态：PREPARED",{exact:false}).waitFor();
  await page.getByRole("button",{name:"确认执行已登记任务一次",exact:true}).click();
  await page.getByText("回执状态：COMMITTED",{exact:false}).waitFor();
  await page.getByRole("link",{name:"查看此Task结果",exact:true}).click();
  await page.getByRole("heading",{name:"A2A Task 只读结果",exact:true}).waitFor();
  const location=new URL(page.url());
  const url=location.hash.startsWith("#/")?new URL(location.hash.slice(1),location.origin):location;
  const task=url.searchParams.get("task"),attempt=url.searchParams.get("attempt");
  assert.ok(task&&attempt);assert.equal(url.searchParams.size,2);
  assert.equal(await page.getByLabel("Task ID",{exact:true}).inputValue(),task);
  assert.equal(await page.getByRole("button",{name:"只读查询 Task",exact:true}).isDisabled(),true);
  await page.getByLabel("用户访问令牌",{exact:true}).fill(tokens.alice_fresh);
  await page.getByRole("button",{name:"应用身份并清空旧结果",exact:true}).click();
  await page.waitForFunction(async ({task,token,extension})=>{
   const r=await fetch('/v1/isolated-a2a/tasks/'+task,{headers:{Authorization:'Bearer '+token,'A2A-Version':'1.0','A2A-Extensions':extension}});
   return r.ok&&(await r.json()).status.state==='TASK_STATE_COMPLETED';
  },{task,token:tokens.alice_fresh,extension},{timeout:10000});
  await page.getByRole("button",{name:"只读查询 Task",exact:true}).click();await page.locator("pre.task-result").waitFor();
  for(const status of [401,403,404,409,503]){
   await page.route(`**${root}/tasks/*`,r=>r.fulfill({status,json:{detail:"synthetic"}}));
   await page.getByRole("button",{name:"只读查询 Task",exact:true}).click();
   await page.getByRole("status").filter({hasText:`HTTP ${status}`}).waitFor();
   assert.equal(await page.locator("pre.task-result").count(),0);
   await page.unroute(`**${root}/tasks/*`);
  }
  await page.getByRole("link",{name:"返回宿主核对回执与本人ACK",exact:true}).click();
  await page.getByRole("heading",{name:"A2A 隔离协议宿主",exact:true}).waitFor();
  await applyHost();assert.equal(await page.getByLabel("恢复 attempt ID（可选）",{exact:true}).inputValue(),attempt);
  await page.getByRole("button",{name:"预览本人ACK",exact:true}).click();
  await page.getByRole("button",{name:"确认本人ACK",exact:true}).click();
  await page.getByText("本人已确认：是",{exact:true}).waitFor();
  assert.deepEqual(await page.evaluate(()=>[localStorage.length,sessionStorage.length]),[0,0]);
 }finally{await context.close();await engine.close();}
});

test("MCP graphical host: execute or withdraw, lost response recovery and explicit ACK", async()=>{
 const tokens=JSON.parse(await readFile(profile.tokens,"utf8"));
 const engine=await browser(),context=await engine.newContext(),page=await context.newPage();
 const calls=[];
 page.on("request",r=>{if(r.url().endsWith("/v1/isolated-mcp")){const b=r.postDataJSON();if(b.method==="tools/call")calls.push(b.params.name);}});
 async function recover(){
  await page.getByLabel("用户访问令牌",{exact:true}).fill(tokens.alice_fresh);
  await page.getByRole("button",{name:"应用身份并清空旧数据",exact:true}).click();
  await page.getByRole("button",{name:"只读核对身份与回执",exact:true}).click();
  await page.waitForFunction(()=>/已核对服务端身份|只读核对失败/.test(document.querySelector('[role="status"]')?.textContent||""));
  assert.match(await page.getByRole("status").innerText(),/已核对服务端身份/);
 }
 try{
  for(const action of ["agent_execute","attempt_withdraw"]){
   await page.goto(profile.url+"/mcp-host");await recover();
   await page.getByRole("heading",{name:"MCP 隔离协议宿主",exact:true}).waitFor();
   await page.getByLabel("可见 Agent",{exact:true}).selectOption("s12-workbench");
   await page.getByLabel("消息",{exact:true}).fill("CREATE-DURABLE-SYNTHETIC");
   await page.getByRole("button",{name:"登记新任务（尚未执行）",exact:true}).click();
   await page.getByText("回执状态：PREPARED",{exact:false}).waitFor();
   await page.route("**/v1/isolated-mcp",async r=>{
    if(r.request().postDataJSON().params?.name!==action)return r.continue();
    const response=await r.fetch();assert.equal(response.status(),200);assert.equal((await response.json()).result.isError,false);await r.abort();
   });
   if(action==="agent_execute")await page.getByRole("button",{name:"确认执行已登记任务一次",exact:true}).click();
   else{
    await page.reload();await recover();
    await page.getByRole("button",{name:"预览撤销登记",exact:true}).click();
    await page.getByRole("button",{name:"确认本人撤销登记",exact:true}).click();
   }
   await page.getByRole("status").filter({hasText:"写结果未知"}).waitFor();
   await page.unroute("**/v1/isolated-mcp");
   await page.reload();await recover();
   await page.getByText("回执状态："+(action==="agent_execute"?"COMMITTED":"REJECTED_NO_EFFECT"),{exact:false}).waitFor();
   await page.getByRole("button",{name:"预览本人ACK",exact:true}).click();
   await page.getByRole("button",{name:"确认本人ACK",exact:true}).click();
   await page.getByText("本人已确认：是",{exact:true}).waitFor();
  }
  assert.equal(calls.filter(n=>n==="agent_prepare").length,2);
  assert.equal(calls.filter(n=>n==="agent_execute").length,1);
  assert.equal(calls.filter(n=>n==="attempt_withdraw").length,1);
  assert.equal(calls.filter(n=>n==="attempt_ack").length,2);
 }finally{await context.close();await engine.close();}
});

test("Observation inbox: signed reporter, unverified costs, admin pages and stale identity isolation",async()=>{
 const tokens=JSON.parse(await readFile(profile.tokens,"utf8"));
 const engine=await browser(),context=await engine.newContext(),page=await context.newPage();
 try{
  await page.goto(profile.url+"/observe-inbox");
  const received=await page.evaluate(async token=>{
   const result=[];
   for(let i=0;i<21;i++){
    const body={event_id:crypto.randomUUID(),kind:"usage",payload:{model:"synthetic",input_tokens:i,output_tokens:0,...(i===20?{cost:0,currency:"CNY"}:{})}};
    const r=await fetch('/v1/isolated-observe/events',{method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify(body)});
    result.push({status:r.status,body:await r.json()});
   }
   return result;
  },tokens.observe_reporter);
  assert.equal(received.length,21);assert.ok(received.every(r=>r.status===200&&r.body.trust==='unverified'&&!r.body.billing_eligible&&!r.body.forwarded));
  async function apply(token){
   await page.getByLabel("管理员测试令牌",{exact:true}).fill(token);
   await page.getByRole("button",{name:"应用身份并清空观测",exact:true}).click();
  }
  await apply(tokens.feedback_admin);
  await page.getByRole("button",{name:"查询观测首页",exact:true}).click();
  await page.getByRole("status").filter({hasText:"只读查询完成"}).waitFor();
  assert.equal(await page.getByRole("article").count(),20);
  assert.equal(await page.getByText("自报费用：未知",{exact:true}).count(),20);
  await page.getByRole("button",{name:"下一页观测",exact:true}).click();
  await page.getByText("自报费用：0 CNY",{exact:true}).waitFor();
  assert.equal(await page.getByRole("article").count(),1);
  assert.equal(await page.getByRole("button",{name:"下一页观测",exact:true}).isDisabled(),true);
  let release,seen;const hold=new Promise(r=>{release=r;});const observed=new Promise(r=>{seen=r;});
  await page.route("**/v1/isolated-observe/events?*",async r=>{const response=await r.fetch();seen();await hold;await r.fulfill({response}).catch(()=>{});});
  await page.getByRole("button",{name:"查询观测首页",exact:true}).click();await observed;
  await apply(tokens.observe_foreign_admin);release();await page.unrouteAll({behavior:"wait"});
  assert.equal(await page.getByRole("article").count(),0);
  await page.getByRole("button",{name:"查询观测首页",exact:true}).click();
  await page.getByRole("status").filter({hasText:"本页无记录"}).waitFor();
  await apply(tokens.alice_fresh);
  await page.getByRole("button",{name:"查询观测首页",exact:true}).click();
  await page.getByRole("status").filter({hasText:"HTTP 403"}).waitFor();
  assert.equal(await page.getByRole("article").count(),0);
  assert.deepEqual(await page.evaluate(()=>[localStorage.length,sessionStorage.length]),[0,0]);
 }finally{await context.close();await engine.close();}
});
