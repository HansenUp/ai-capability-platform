import test from "node:test";
import assert from "node:assert/strict";
import { readFile, writeFile } from "node:fs/promises";
import { apply, browser, phase, profile } from "./support.mjs";

test("工单总结只读隔离演示：配置→发布→同 Run 对话→轨迹", async () => {
  const tokens = JSON.parse(await readFile(profile.tokens, "utf8"));
  const engine = await browser(), context = await engine.newContext(), page = await context.newPage();
  const external = [], apiWrites = [];
  page.setDefaultTimeout(5000);
  await context.route("**/*", async route => {
    if (new URL(route.request().url()).origin !== new URL(profile.url).origin) { external.push(route.request().url()); await route.abort(); }
    else await route.continue();
  });
  page.on("request", request => {
    const path = new URL(request.url()).pathname;
    if (request.method() === "POST" && path.startsWith("/v1/")) apiWrites.push(path);
  });
  const agent = `ticket-readonly-${Date.now()}`;
  try {
    await page.goto(`${profile.url}/demo`);
    await page.getByRole("heading", { name: "工单总结 · 只读隔离演示", exact: true }).waitFor();
    assert.equal(await page.getByText("SYNTHETIC_TICKET_SUMMARY", { exact: true }).count(), 1);
    assert.equal(await page.getByText("ticket.close_batch", { exact: false }).count(), 0);
    await page.goto(`${profile.url}/manage/agents`);
    await page.getByLabel("Agent ID", { exact: true }).fill(agent);
    await page.getByLabel("版本", { exact: true }).fill("1");
    await page.getByLabel("完整 config JSON（高级编辑，保留扩展字段）", { exact: true }).fill(JSON.stringify({ tools: ["ticket.get"] }));
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
    await page.goto(`${profile.url}/chat`);
    await apply(page, tokens.alice);
    await page.getByLabel("Agent ID", { exact: true }).fill(agent);
    await page.getByLabel("消息", { exact: true }).fill("SYNTHETIC_TICKET_SUMMARY");
    const registered = page.waitForResponse(response => new URL(response.url()).pathname === "/v1/workbench/create-attempts" && response.request().method() === "POST");
    await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).click();
    const registration = await registered; assert.equal(registration.status(), 200);
    const prepared = await registration.json(), runId = prepared.run_id;
    const created = page.waitForResponse(response => new URL(response.url()).pathname === `/v1/workbench/attempts/${prepared.attempt_id}/execute`);
    await page.getByRole("button", { name: "确认执行已登记创建一次", exact: true }).click();
    assert.equal((await created).status(), 200);
    await page.getByRole("button", { name: "预览本人确认收口", exact: true }).click();
    await page.getByRole("button", { name: "确认收口此回执", exact: true }).click();
    await phase(page, runId, "completed", tokens.alice);
    await page.getByText("助手：模拟工单摘要：SYNTHETIC-1 / 设备无法连接 / open", { exact: true }).waitFor();
    const response = await page.request.get(`${profile.runtime}/v1/runs/${runId}/events`, { headers: { Authorization: `Bearer ${tokens.alice}` } });
    assert.equal(response.status(), 200);
    const events = await response.json();
    assert.equal(events.filter(event => event.type === "run.created").length, 1);
    const requested = events.filter(event => event.type === "tool.requested");
    assert.equal(requested.length, 1); assert.equal(requested[0].payload.tool, "ticket.get");
    const toolResult = events.find(event => event.type === "tool.result");
    assert.equal(toolResult.payload.output.id, "SYNTHETIC-1");
    assert.deepEqual(toolResult.payload.output, { id: "SYNTHETIC-1", title: "设备无法连接", status: "open" });
    assert.equal(events.some(event => event.type === "model.response" && event.payload.text === "模拟工单摘要：SYNTHETIC-1 / 设备无法连接 / open"), true);
    assert.equal(events.some(event => ["tool.approved", "input.provided"].includes(event.type)), false);
    await page.getByLabel("消息", { exact: true }).fill("SYNTHETIC_TICKET_FAILURE");
    const failedRegistration = page.waitForResponse(response => new URL(response.url()).pathname === "/v1/workbench/create-attempts" && response.request().method() === "POST");
    await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).click();
    const failedPrepared = await (await failedRegistration).json();
    const failedExecution = page.waitForResponse(response => new URL(response.url()).pathname === `/v1/workbench/attempts/${failedPrepared.attempt_id}/execute`);
    await page.getByRole("button", { name: "确认执行已登记创建一次", exact: true }).click();
    assert.equal((await failedExecution).status(), 200);
    await page.getByRole("button", { name: "预览本人确认收口", exact: true }).click();
    await page.getByRole("button", { name: "确认收口此回执", exact: true }).click();
    await phase(page, failedPrepared.run_id, "completed", tokens.alice);
    const failedResponse = await page.request.get(`${profile.runtime}/v1/runs/${failedPrepared.run_id}/events`, { headers: { Authorization: `Bearer ${tokens.alice}` } });
    assert.equal(failedResponse.status(), 200);
    const failedEvents = await failedResponse.json();
    assert.equal(failedEvents.some(event => event.type === "tool.result" && event.payload.status === "error"), true);
    assert.equal(failedEvents.some(event => event.type === "model.response" && event.payload.text === "模拟工单查询不可用，未生成摘要。"), true);
    assert.equal(failedEvents.some(event => event.type === "model.response" && event.payload.text.startsWith("模拟工单摘要：")), false);
    await page.goto(`${profile.url}/manage/runs`);
    await page.getByLabel("访问令牌", { exact: true }).fill(tokens.alice);
    await page.getByRole("button", { name: "应用读取身份并清空旧数据", exact: true }).click();
    await page.getByLabel("Run ID", { exact: true }).fill(runId);
    await page.getByRole("button", { name: "读取指定 Run", exact: true }).click();
    await page.getByRole("heading", { name: `${runId} · completed`, exact: true }).waitFor();
    const eventNames = await page.locator("section ol li > b").allTextContents();
    for (const name of ["run.created", "tool.requested", "tool.result", "model.response"])
      assert.equal(eventNames.some(text => text.endsWith(name)), true, name);
    assert.equal(apiWrites.some(path => /\/(approve|reject|input|resume)$/.test(path)), false);
    assert.equal(await page.getByRole("link", { name: "在本机 Langfuse 查看 Trace", exact: true }).count(), 0);
    assert.deepEqual(external, []);
    assert.deepEqual(await page.evaluate(() => [localStorage.getItem("token"), sessionStorage.length]), [null, 0]);
    await page.screenshot({ path: `${process.env.S12_BROWSER_EVIDENCE}/ticket-readonly-trace.png`, fullPage: true });
    await writeFile(`${process.env.S12_BROWSER_EVIDENCE}/result.json`, JSON.stringify({ agent, run_id: runId, scenario: "ticket-summary-readonly",
      stages: ["draft", "published_dev_flag", "completed", "trace"], tool: "ticket.get", external_effect: false,
      real_model: false, real_identity: false, real_evaluation: false, trace_events: eventNames.length }, null, 2));
  } finally { await context.close(); await engine.close(); }
});
