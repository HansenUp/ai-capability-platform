import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";
const source = readFileSync(new URL("../../../packages/sdk-web/src/run-client.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
const sdk = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`);
const event = (seq, type, payload) => ({ run_id: "run1", seq, type, payload });
const run = { run_id: "run1", state: "auth-required" };
const pending = [event(1, "tool.requested", { call_id: "c", tool: "synthetic", input: { value: 1 } }),
  event(2, "tool.decision", { call_id: "c", decision: "require_approval", params_hash: "hash" })];
test("SDK 创建直接走 Runtime、只发送一次且不注入 Console 凭据", async () => {
  const calls = [];
  const client = new sdk.RuntimeRunClient(async () => ({ Authorization: "Bearer synthetic" }), async (...args) => {
    calls.push(args); return Response.json({ run_id: "run1", state: "submitted" }, { status: 202 });
  });
  assert.equal(await client.create("demo", "hello"), "run1");
  assert.equal(calls.length, 1); assert.equal(calls[0][0], "/v1/runs");
  assert.deepEqual(JSON.parse(calls[0][1].body), { agent_id: "demo", input: [{ role: "user", content: "hello" }] });
  assert.equal(calls[0][1].headers["X-Console-Role"], undefined); assert.equal(calls[0][1].redirect, "error");
});
test("SDK fetch 不以客户端实例作为浏览器原生 fetch 的接收者", async () => {
  const client = new sdk.RuntimeRunClient(async () => ({}), function () {
    assert.equal(this, undefined);
    return Promise.resolve(Response.json({ run_id: "run1" }, { status: 202 }));
  });
  assert.equal(await client.create("demo", "hello"), "run1");
});
test("审批与拒绝原样绑定 call_id 和 params_hash，不携带伪造审批身份", async () => {
  const calls = [], client = new sdk.RuntimeRunClient(async () => ({}), async (...args) => { calls.push(args); return Response.json({}); });
  const card = sdk.workbenchCards(run, pending).approvals[0];
  await client.decide("run1", card, true); await client.decide("run1", card, false);
  assert.equal(calls[0][0], "/v1/runs/run1/approve"); assert.equal(calls[1][0], "/v1/runs/run1/reject");
  assert.deepEqual(JSON.parse(calls[0][1].body), { call_id: "c", params_hash: "hash" });
  await assert.rejects(client.decide("run1", { call_id: "c", params_hash: "" }, true));
});
test("非挂起态不显示审批；部分批次已批后不重复展示", () => {
  assert.equal(sdk.workbenchCards({ ...run, state: "completed" }, pending).approvals.length, 0);
  assert.equal(sdk.workbenchCards(run, [...pending, event(3, "tool.approved", { call_id: "c" })]).approvals.length, 0);
  assert.equal(sdk.workbenchCards(run, [...pending, event(3, "tool.result", { call_id: "c" })]).approvals.length, 0);
  assert.equal(sdk.workbenchCards(run, [pending[0]]).approvals.length, 0);
});
test("输入卡片遵循 input-required 与最后待答状态", () => {
  const asked = [event(1, "input.requested", { kind: "answer", prompt: "id?", call_id: "ask", schema: { type: "string" } })];
  assert.equal(sdk.workbenchCards({ ...run, state: "input-required" }, asked).input.call_id, "ask");
  assert.equal(sdk.workbenchCards(run, asked).input, null);
  assert.equal(sdk.workbenchCards({ ...run, state: "input-required" }, [...asked, event(2, "input.provided", {})]).input, null);
});
test("自由文本与结构化 JSON 回答分离，无效 JSON 不发请求", () => {
  assert.equal(sdk.answerValue({ kind: "answer" }, "hello"), "hello");
  assert.deepEqual(sdk.answerValue({ kind: "answer", schema: {} }, '{"id":"T1"}'), { id: "T1" });
  assert.throws(() => sdk.answerValue({ schema: {} }, "invalid")); assert.throws(() => sdk.answerValue({}, " "));
});
test("凭据只通过当前身份头，不放 input 请求体；对账写不提供入口", async () => {
  const calls = [], client = new sdk.RuntimeRunClient(async () => ({ Authorization: "Bearer synthetic" }), async (...args) => { calls.push(args); return Response.json({}); });
  await client.input("run1", { kind: "credential" }, "must-not-send");
  assert.deepEqual(JSON.parse(calls[0][1].body), { kind: "credential" });
  await assert.rejects(client.input("run1", { kind: "reconcile" }, {})); assert.equal(calls.length, 1);
});
test("401／403／409 错误不回显响应正文也不自动重试", async () => {
  for (const status of [401, 403, 409]) {
    let calls = 0;
    const client = new sdk.RuntimeRunClient(async () => ({}), async () => { calls++; return new Response("secret", { status }); });
    await assert.rejects(client.decide("run1", { call_id: "c", params_hash: "h" }, true), error => error.status === status && !error.message.includes("secret"));
    assert.equal(calls, 1);
  }
});
test("写响应丢失与 5xx 标为未知，损坏 Run ID 也不可重发", async () => {
  for (const responder of [() => { throw new Error("network"); }, () => new Response("", { status: 503 }), () => Response.json(null)]) {
    let calls = 0;
    const client = new sdk.RuntimeRunClient(async () => ({}), async () => { calls++; return responder(); });
    await assert.rejects(client.create("demo", "text"), error => error.outcomeUnknown === true); assert.equal(calls, 1);
  }
});
test("读取拒绝跨 Run 事件与不可安全表示的序号", async () => {
  for (const events of [[{ run_id: "foreign", seq: 1 }], [{ run_id: "run1", seq: Infinity }]]) {
    let count = 0;
    const client = new sdk.RuntimeRunClient(async () => ({}), async () => Response.json(count++ ? events : { run_id: "run1" }));
    await assert.rejects(client.inspect("run1"));
  }
});
test("SDK 只读取消：等待身份期间取消，不再发出任何请求", async () => {
  let release, count = 0;
  const barrier = new Promise(resolve => { release = resolve; });
  const controller = new AbortController();
  const client = new sdk.RuntimeRunClient(async () => { await barrier; return { Authorization: "Bearer synthetic" }; }, async () => { count++; return Response.json(count === 1 ? { run_id: "run1" } : []); });
  const pending = client.inspect("run1", controller.signal);
  controller.abort(); release();
  await assert.rejects(pending, error => error.name === "AbortError"); assert.equal(count, 0);
});
test("SDK 只读取消：传输忽略取消、旧详情 body 迟到，也不继续 events", async () => {
  let release, entered, count = 0;
  const barrier = new Promise(resolve => { release = resolve; });
  const ready = new Promise(resolve => { entered = resolve; });
  const controller = new AbortController();
  const client = new sdk.RuntimeRunClient(async () => ({}), async () => {
    count++;
    return count === 1 ? { ok: true, status: 200, json: async () => { entered(); await barrier; return { run_id: "run1" }; } } : Response.json([]);
  });
  const pending = client.inspect("run1", controller.signal); await ready; controller.abort(); release();
  await assert.rejects(pending, error => error.name === "AbortError"); assert.equal(count, 1);
});
test("SDK 范围错误先拒绝元数据，不再请求事件", async () => {
  let count = 0;
  const client = new sdk.RuntimeRunClient(async () => ({}), async () => { count++; return Response.json(count === 1 ? { run_id: "foreign" } : []); });
  await assert.rejects(client.inspect("run1")); assert.equal(count, 1);
});
test("SDK 事件契约：正序正整数与对象负载，保留受限视图序号缺口", async () => {
  const valid = event(1, "model.response", { text: "synthetic" });
  const invalid = [[{ ...valid, seq: 0 }], [{ ...valid, seq: -1 }], [{ ...valid, payload: null }], [{ ...valid, payload: [] }], [{ ...valid, type: null }], [valid, valid], [{ ...valid, seq: 2 }, valid]];
  for (const rows of invalid) {
    let count = 0;
    const client = new sdk.RuntimeRunClient(async () => ({}), async () => Response.json(count++ ? rows : { run_id: "run1" }));
    await assert.rejects(client.inspect("run1"));
  }
  let count = 0;
  const client = new sdk.RuntimeRunClient(async () => ({}), async () => Response.json(count++ ? [valid, { ...valid, seq: 5 }] : { run_id: "run1" }));
  assert.equal((await client.inspect("run1")).events.length, 2);
});
