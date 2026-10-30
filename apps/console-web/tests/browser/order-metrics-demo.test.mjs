import {copyFile} from "node:fs/promises";
import test from "node:test";
import assert from "node:assert/strict";
import {readFile,writeFile} from "node:fs/promises";
import {parseOrderPack} from "../../src/order-scenario.ts";
import {apply,browser,phase,profile} from "./support.mjs";
const pack=parseOrderPack(await readFile(new URL("../../src/scenarios/order-metrics.json",import.meta.url),"utf8"));
test("经营包→发布→订单聚合→失败/空结果→ACK与身份隔离",async()=>{
 const tokens=JSON.parse(await readFile(profile.tokens,"utf8"));
 const engine=await browser(),context=await engine.newContext(),page=await context.newPage();
 const apiWrites=[];page.setDefaultTimeout(5000);
 await context.route("**/*",route=>new URL(route.request().url()).origin===new URL(profile.url).origin?route.continue():route.abort());
 page.on("request",r=>{if(r.method()==="POST"&&new URL(r.url()).pathname.startsWith("/v1/"))apiWrites.push(r.url());});
 const agent="analytics-"+Date.now();
 try {
    await page.goto(`${profile.url}/manage/agents`);
    await page.getByLabel("Agent ID", { exact: true }).fill(agent);
    await page.getByLabel("版本", { exact: true }).fill("1");
    const beforeImport=apiWrites.length;
    await page.getByRole("button",{name:"载入内置经营场景包",exact:true}).click();
    const invalid=structuredClone(pack);invalid.agent.config.tools.push("sales.close_batch");
    await page.getByLabel("场景包 JSON",{exact:true}).fill(JSON.stringify(invalid));
    await page.getByRole("button",{name:"校验场景包",exact:true}).click();
    await page.getByRole("status").filter({hasText:"只允许analytics.orders"}).waitFor();
    assert.equal(await page.getByRole("button",{name:"确认填入草稿配置",exact:true}).count(),0);
    await page.getByRole("button",{name:"载入内置经营场景包",exact:true}).click();
    await page.getByRole("button",{name:"校验场景包",exact:true}).click();
    await page.getByRole("region",{name:"场景包预览",exact:true}).waitFor();
    await page.getByRole("button",{name:"确认填入草稿配置",exact:true}).click();
    assert.equal(await page.getByLabel("Harness",{exact:true}).inputValue(),"mock");
    assert.deepEqual(JSON.parse(await page.getByLabel("完整 config JSON（高级编辑，保留扩展字段）",{exact:true}).inputValue()),{tools:["analytics.orders"]});
    assert.equal(apiWrites.length,beforeImport);
    await page.getByRole("button", { name: "仅保存草稿", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "草稿已保存" }).waitFor();
    const row = page.getByRole("row").filter({ hasText: agent });
    assert.match(await row.innerText(), /draft/);
    await row.getByRole("button", { name: "评测与发布", exact: true }).click();
    await page.getByText("当前为开发 flag 模式，不会伪称 Langfuse 真实评测通过。", { exact: true }).waitFor();
    await page.getByLabel("显式确认开发态评测通过（非真实评测验收）", { exact: true }).check();
    await page.getByRole("button", { name: "检查并发布", exact: true }).click();
    await page.getByRole("heading", { name: "确认变更", exact: true }).waitFor();
    await page.getByRole("button", { name: "确认执行上述变更", exact: true }).click();
    await row.filter({ hasText: "published" }).waitFor();
    await row.getByRole("link", {name:"在工作台使用",exact:true}).click();
    await page.getByRole("button", {name:"使用发布页 Agent（不创建任务）",exact:true}).waitFor();
    assert.equal(await page.getByRole("button", {name:"使用发布页 Agent（不创建任务）",exact:true}).isDisabled(), true);
    await apply(page, tokens.alice);
    await page.getByRole("button", {name:"使用发布页 Agent（不创建任务）",exact:true}).click();
    assert.equal(await page.getByLabel("Agent ID", {exact:true}).inputValue(), agent);
    await page.getByLabel("Agent ID", { exact: true }).fill(agent);
    await page.getByLabel("消息", { exact: true }).fill("SYNTHETIC_ORDER_SUMMARY");
    const registered = page.waitForResponse(response => new URL(response.url()).pathname === "/v1/workbench/create-attempts" && response.request().method() === "POST");
    await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).click();
    const registration = await registered; assert.equal(registration.status(), 200);
    const prepared = await registration.json(), runId = prepared.run_id;
    const created = page.waitForResponse(response => new URL(response.url()).pathname === `/v1/workbench/attempts/${prepared.attempt_id}/execute`);
    await page.getByRole("button", { name: "确认执行已登记创建一次", exact: true }).click();
    assert.equal((await created).status(), 200);
    await page.getByRole("button", { name: "预览本人确认收口", exact: true }).click();
    await page.getByRole("button", { name: "确认收口此回执", exact: true }).click();
    try {await phase(page, runId, "completed", tokens.alice);}catch(error){
      const diagnostics=await page.request.get(`${profile.runtime}/v1/runs/${runId}/events`,{headers:{Authorization:`Bearer ${tokens.alice}`}});
      console.error("SYNTHETIC_EVENTS",await diagnostics.json());throw error;
    }
    await page.getByText(/助手：模拟经营摘要：/).waitFor();
    await page.getByRole("region", { name: "任务结果摘要" }).getByText("可见工具结果事件：成功 1 · 失败或拒绝 0 · 状态未知 0", { exact: true }).waitFor();
    const usage = page.getByRole("region", {name:"模型用量与预算",exact:true});
    await usage.getByText(/实际费用：未知/).waitFor();
    await usage.getByRole("region", {name:"用量统计覆盖范围",exact:true}).getByText(/不提供全 Run 或账户总消费/).waitFor();
    assert.equal(await usage.getByRole("button").count(), 0);
    const response=await page.request.get(`${profile.runtime}/v1/runs/${runId}/events`,{headers:{Authorization:`Bearer ${tokens.alice}`}});
    assert.equal(response.status(),200);
    const events=await response.json();
    const output=events.find(e=>e.type==="tool.result").payload.output;
    assert.equal(output.order_count,3);
    assert.deepEqual(output.currencies.map(g=>g.total_minor),["12000","500"]);
    assert.equal(JSON.stringify(output).includes("NEVER-MODEL"),false);
    assert.equal(JSON.stringify(output).includes("order_id"),false);
    await page.getByRole("region",{name:"经营指标",exact:true}).getByText(/订单数：3/).waitFor();
    for(const sample of pack.cases.slice(1)){
      await page.getByLabel("消息",{exact:true}).fill(sample.message);
      const registered=page.waitForResponse(r=>new URL(r.url()).pathname==="/v1/workbench/create-attempts"&&r.request().method()==="POST");
      await page.getByRole("button",{name:"登记新 Run（尚未创建）",exact:true}).click();
      const response=await registered;assert.equal(response.status(),200);const attempt=await response.json();
      const executed=page.waitForResponse(r=>new URL(r.url()).pathname===`/v1/workbench/attempts/${attempt.attempt_id}/execute`);
      await page.getByRole("button",{name:"确认执行已登记创建一次",exact:true}).click();assert.equal((await executed).status(),200);
      await page.getByRole("button",{name:"预览本人确认收口",exact:true}).click();
      await page.getByRole("button",{name:"确认收口此回执",exact:true}).click();
      await phase(page,attempt.run_id,"completed",tokens.alice);
      const res=await page.request.get(`${profile.runtime}/v1/runs/${attempt.run_id}/events`,{headers:{Authorization:`Bearer ${tokens.alice}`}});
      const rows=await res.json();
      assert.equal(rows.find(e=>e.type==="tool.result").payload.status,sample.tool_status);
      assert.equal(rows.find(e=>e.type==="tool.result").payload.output.order_count,Number(sample.expected_text));
      await page.getByRole("region",{name:"经营指标",exact:true}).waitFor();
      if(sample.id==="incomplete") {assert.equal(rows.find(e=>e.type==="tool.result").payload.output.currencies[0].total_minor,null);assert.equal(await page.getByRole("cell",{name:"未知",exact:true}).count(),2);}

    }
    const denied=await page.request.get(`${profile.runtime}/v1/runs/${runId}/events`,{headers:{Authorization:`Bearer ${tokens.bob}`}});
    assert.equal(denied.status(),403);
    const adminDenied=await page.request.get(`${profile.runtime}/v1/runs/${runId}/events`,{headers:{Authorization:`Bearer ${tokens.feedback_admin}`}});
    assert.equal(adminDenied.status(),403);
    const adminList=await page.request.get(`${profile.runtime}/v1/runs?agent_id=${agent}`,{headers:{Authorization:`Bearer ${tokens.feedback_admin}`}});
    assert.equal(adminList.status(),200);
    assert.deepEqual((await adminList.json()).items,[]);
    await apply(page,tokens.bob);
    assert.equal(await page.getByText(/助手：模拟经营摘要：/).count(),0);

    await page.getByLabel("Agent ID",{exact:true}).fill(agent);
    await page.getByLabel("消息",{exact:true}).fill("SYNTHETIC_ORDER_SUMMARY");
    const reg=page.waitForResponse(r=>new URL(r.url()).pathname==="/v1/workbench/create-attempts"&&r.request().method()==="POST");
    await page.getByRole("button",{name:"登记新 Run（尚未创建）",exact:true}).click();
    const attempt=await (await reg).json();
    const ex=page.waitForResponse(r=>new URL(r.url()).pathname===`/v1/workbench/attempts/${attempt.attempt_id}/execute`);
    await page.getByRole("button",{name:"确认执行已登记创建一次",exact:true}).click();assert.equal((await ex).status(),200);
    await page.getByRole("button",{name:"预览本人确认收口",exact:true}).click();
    await page.getByRole("button",{name:"确认收口此回执",exact:true}).click();
    await phase(page,attempt.run_id,"completed",tokens.bob);
    const rejected=await page.request.get(`${profile.runtime}/v1/runs/${attempt.run_id}/events`,{headers:{Authorization:`Bearer ${tokens.bob}`}});
    const deniedEvents=await rejected.json();
    assert.equal(deniedEvents.some(e=>e.type==="tool.result"&&e.payload.status==="error"),true);
    assert.equal(await page.getByRole("region",{name:"经营指标",exact:true}).count(),0);
    const beforeRead=apiWrites.length;
    await page.goto(`${profile.url}/manage/runs`);
    await page.getByLabel("访问令牌",{exact:true}).fill(tokens.alice);
    await page.getByRole("button",{name:"应用读取身份并清空旧数据",exact:true}).click();
    await page.getByLabel("Run ID",{exact:true}).fill(runId);
    await page.getByRole("button",{name:"读取指定 Run",exact:true}).click();
    await page.getByRole("heading",{name:`${runId} · completed`,exact:true}).waitFor();
    const names=await page.locator("section ol li > b").allTextContents();
    for(const name of ["run.created","tool.requested","tool.result","model.response"])
      assert.equal(names.some(t=>t.endsWith(name)),true);
    assert.equal(apiWrites.length,beforeRead);
    await page.screenshot({path:`${process.env.S12_BROWSER_EVIDENCE}/analytics-readonly-trace.png`,fullPage:true});
    await writeFile(`${process.env.S12_BROWSER_EVIDENCE}/result.json`,JSON.stringify({
      scenario:"order-metrics-readonly",agent,run_id:runId,stages:["draft","published_dev_flag","completed","trace"],
      synthetic_evaluation:{success:true,empty:true,incomplete:true},external_effect:false,real_model:false,real_identity:false,real_evaluation:false
    },null,2));
 await copyFile(`${process.env.S12_BROWSER_EVIDENCE}/result.json`, `${process.env.S12_BROWSER_EVIDENCE}/orders-result.json`);
  }finally{await context.close();await engine.close();}
});
