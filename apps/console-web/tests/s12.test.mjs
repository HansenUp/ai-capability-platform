import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";

const source = readFileSync(new URL("../src/operations.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
const operations = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`);
const operator = { user: "ops", role: "TENANT_ADMIN", tenant: "tenant-a" };

test("Trace 时间窗规范化为 UTC、Agent 编码、拒绝空值和超七天", () => {
  const path = operations.traceQuery(" a/b ", "2026-10-21T08:00:00+08:00", "2026-10-22T08:00:00+08:00");
  const url = new URL(path, "http://localhost");
  assert.equal(url.searchParams.get("agent_id"), "a/b");
  assert.equal(url.searchParams.get("since"), "2026-10-21T00:00:00.000Z");
  for (const args of [["", "2026-10-21", "2026-10-22"], ["a", "invalid", "invalid"], ["a", "2026-10-21", "2026-10-21"],
    ["a", "2026-10-22", "2026-10-21"], ["a", "2026-10-01", "2026-10-09"]]) assert.throws(() => operations.traceQuery(...args));
});
test("缺失用量不补零，十进制费用字符串不丢精度", () => {
  for (const value of [null, undefined, NaN, Infinity, -1, "NaN", true]) assert.equal(operations.metric(value), "未知");
  assert.equal(operations.metric(0), "0");
  assert.equal(operations.metric("0.000000000123456789"), "0.000000000123456789");
});
test("回执查询编码路径且不使用节点拉取或 ACK 入口", () => {
  assert.equal(operations.commandPath("n/a", "id?x=1"), "/nodes/n%2Fa/commands/id%3Fx%3D1");
  assert.throws(() => operations.commandPath("", "id"));
});
test("只读请求 no-store、GET 和当前租户身份，不包含节点凭据", async () => {
  const calls = [];
  const session = operations.readSession(true, async (...args) => { calls.push(args); return Response.json({ agents: 1 }); });
  assert.deepEqual(await session.read("/metrics/operations", operator), { agents: 1 });
  assert.equal(calls[0][0], "/console/metrics/operations");
  assert.equal(calls[0][1].method, "GET");
  assert.equal(calls[0][1].cache, "no-store");
  assert.equal(calls[0][1].headers["X-Console-Tenant"], "tenant-a");
  assert.equal(calls[0][1].headers.Authorization, undefined);
});
test("生产构建失败关闭，不发送开发身份头", async () => {
  let called = false;
  const session = operations.readSession(false, async () => { called = true; return Response.json({}); });
  await assert.rejects(session.read("/metrics/operations", operator), /生产统一登录/);
  assert.equal(called, false);
});
test("HTTP 错误不展示后端敏感响应", async () => {
  const session = operations.readSession(true, async () => new Response("secret source payload", { status: 503 }));
  await assert.rejects(session.read("/metrics/traces", operator), error => /503/.test(error.message) && !error.message.includes("secret"));
});
test("身份或条件变更后即使源忽略 abort，也拒绝旧响应", async () => {
  let resolve;
  const session = operations.readSession(true, () => new Promise(done => { resolve = done; }));
  const pending = session.read("/metrics/operations", operator);
  session.invalidate();
  resolve(Response.json({ tenant_id: "tenant-a" }));
  await assert.rejects(pending, { name: "AbortError" });
});
test("重叠读取只允许最新请求完成", async () => {
  const resolvers = [];
  const session = operations.readSession(true, () => new Promise(done => resolvers.push(done)));
  const first = session.read("/metrics/operations", operator);
  const second = session.read("/metrics/operations", { ...operator, tenant: "tenant-b" });
  resolvers[1](Response.json({ tenant_id: "tenant-b" }));
  assert.deepEqual(await second, { tenant_id: "tenant-b" });
  resolvers[0](Response.json({ tenant_id: "tenant-a" }));
  await assert.rejects(first, { name: "AbortError" });
});
