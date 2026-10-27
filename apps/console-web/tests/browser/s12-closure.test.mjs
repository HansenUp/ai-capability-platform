import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import { apply, browser, profile, mockLedgerRead, ledgerIdentity } from "./support.mjs";
import { held, hold, installReadBarrier, mountReadOnly, releaseHeld, traceForm, traces } from "./readonly-fixture.mjs";
let engine;
before(async () => { engine = await browser(); });
after(async () => { await engine?.close(); });
async function chat(action) {
  const context = await engine.newContext(); await installReadBarrier(context);
  const page = await context.newPage(); page.setDefaultTimeout(5000);
  const calls = [];
  await context.route("**/v1/**", async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    calls.push({ path, method: request.method(), headers: request.headers() });
    if (await mockLedgerRead(route)) return;
    if (request.method() === "POST" && path === "/v1/workbench/create-attempts") return route.fulfill({ json: {
      scope_id: ledgerIdentity.scope_id, attempt_id: request.postDataJSON().attempt_id, action: "create", run_id: "run_" + "a".repeat(20), request_seq: 0,
      create_agent: "demo", create_version: "1", create_model: "default-chat", create_reason: "only", create_snapshot: "c".repeat(64),
      value_hmac: "d".repeat(64), fingerprint: "b".repeat(64), status: "PREPARED", revision: 1, acknowledged: false, evidence_seq: null, rejection: null,
    } });
    if (request.method() !== "GET") return route.fulfill({ status: 405 });
    if (path.endsWith("/events")) return route.fulfill({ json: [] });
    return route.fulfill({ json: { run_id: "synthetic-run", state: "completed", subject: "human:alice", agent_id: "demo", last_seq: 1 } });
  });
  try {
    await page.goto(`${profile.url}/tests/browser/workbench.html`); await apply(page, "synthetic-token");
    await action(page, calls);
  } finally { await context.close(); }
}
test("S12.3：只取消读取、不取消 Run；旧 body 迟到不能覆盖新身份或继续 events", async () => chat(async (page, calls) => {
  await page.evaluate(() => { window.syntheticTransport.holdPath = "/v1/runs/synthetic-run"; });
  await page.getByLabel("Run ID", { exact: true }).fill("synthetic-run");
  await hold(page); await page.getByRole("button", { name: "读取 Run 与待办卡片", exact: true }).click(); await held(page);
  await page.getByRole("button", { name: "停止本次只读刷新", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "未取消 Run" }).waitFor();
  await apply(page, "next-synthetic-token");
  await page.evaluate(() => { window.syntheticTransport.hold = false; });
  await page.getByLabel("Run ID", { exact: true }).fill("synthetic-run");
  await page.getByRole("button", { name: "读取 Run 与待办卡片", exact: true }).click();
  await page.getByRole("heading", { name: /synthetic-run · completed/ }).waitFor();
  await releaseHeld(page);
  assert.equal(calls.filter(row => row.path.endsWith("/events")).length, 1);
  assert.equal(calls.at(-1).headers.authorization, "Bearer next-synthetic-token");
  assert.equal(calls.every(row => row.method === "GET"), true);
  assert.equal(await page.getByRole("heading", { name: /synthetic-run · completed/ }).count(), 1);
}));
test("S12.3：创建登记结果迟到时卸载，不以已清空的身份继续读取或执行", async () => chat(async (page, calls) => {
  await page.evaluate(() => { window.syntheticTransport.holdPath = "/v1/workbench/create-attempts"; });
  await hold(page); await page.getByLabel("消息", { exact: true }).fill("synthetic message");
  await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).click(); await held(page);
  await page.locator("#unmount").click(); await releaseHeld(page);
  assert.deepEqual(calls.map(row => [row.method, row.path]), [["GET", "/v1/workbench/identity"], ["GET", "/v1/workbench/attempts"],
    ["GET", "/v1/workbench/identity"], ["GET", "/v1/workbench/attempts"], ["POST", "/v1/workbench/create-attempts"]]);
  assert.equal(await page.evaluate(() => window.syntheticTransport.paths.length), 5);
}));
test("S12.1：Trace 响应时间窗须匹配请求，等价时区可接受，不显示错误范围统计", async () => mountReadOnly(engine, "operations", async ({ page, handle }) => {
  let mismatch = false;
  handle(async route => { await route.fulfill({ json: { ...traces(), since: mismatch ? "2026-10-20T08:00:00+08:00" : "2026-10-21T08:00:00+08:00", until: "2026-10-21T09:00:00+08:00" } }); return true; });
  await traceForm(page); await page.getByRole("button", { name: "查询 Trace 指标", exact: true }).click();
  await page.getByText("0.000000123456789 / 1", { exact: true }).waitFor();
  mismatch = true; await page.getByRole("button", { name: "查询 Trace 指标", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "时间窗不匹配" }).waitFor();
  assert.equal(await page.getByText("0.000000123456789 / 1", { exact: true }).count(), 0);
}));
