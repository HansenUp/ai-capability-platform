import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import { apply, browser, profile, mockLedgerRead, ledgerIdentity } from "./support.mjs";
let engine;
before(async () => { engine = await browser(); });
after(async () => { await engine?.close(); });
const run = { run_id: "component-run", agent_id: "demo", state: "auth-required", subject: "human:alice", last_seq: 2 };
const approval = [
  { run_id: run.run_id, seq: 1, type: "tool.requested", payload: { call_id: "call", tool: "synthetic", input: { safe: true } } },
  { run_id: run.run_id, seq: 2, type: "tool.decision", payload: { call_id: "call", params_hash: "original-hash", decision: "require_approval" } },
];
async function fixture(action) {
  const context = await engine.newContext();
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  const calls = [];
  let current = { ...run }, events = approval;
  let ledger = null;
  let mutation = async route => route.fulfill({ status: 200, json: {} });
  await page.route("**/v1/**", async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    calls.push({ path, method: request.method(), body: request.postDataJSON(), headers: request.headers() });
    if (request.method() === "GET" && path === "/v1/workbench/attempts") return route.fulfill({ json: { scope_id: ledgerIdentity.scope_id, active: ledger } });
    if (await mockLedgerRead(route)) return;
    if (request.method() !== "GET") return mutation(route);
    if (path.endsWith("/events")) return route.fulfill({ json: events });
    return route.fulfill({ json: current });
  });
  await page.goto(`${profile.url}/tests/browser/workbench.html`);
  await page.getByRole("heading", { name: "使用工作台 · 本机最小闭环", exact: true }).waitFor();
  try { await action({ page, calls, state: (value, rows) => { current = value; events = rows; }, ledger: value => { ledger = value; }, mutate: handler => { mutation = handler; } }); }
  finally { await context.close(); }
}
async function inspect(page) {
  await page.getByLabel("Run ID", { exact: true }).fill(run.run_id);
  await page.getByRole("button", { name: "读取 Run 与待办卡片", exact: true }).click();
}
test("真实组件：身份应用清空令牌、Run 定位与权限失败清空卡片", async () => fixture(async ({ page, calls, mutate }) => {
  assert.equal(await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), true);
  await apply(page, "component-token"); await inspect(page);
  await page.getByRole("heading", { name: "审批卡片：synthetic", exact: true }).waitFor();
  assert.equal(calls[0].headers.authorization, "Bearer component-token");
  assert.equal(calls[0].headers["x-console-role"], undefined);
  await page.getByRole("button", { name: "预览批准", exact: true }).click();
  assert.equal(calls.filter(row => row.method === "POST").length, 0);
  mutate(route => route.fulfill({ status: 403, body: "sensitive server body" }));
  await page.getByRole("button", { name: "确认登记一次", exact: true }).click();
  await page.getByText(/Runtime HTTP 403/).waitFor();
  assert.equal(await page.getByRole("heading", { name: "审批卡片：synthetic", exact: true }).count(), 0);
  assert.equal((await page.locator("body").innerText()).includes("sensitive server body"), false);
  assert.deepEqual(calls.at(-1).body, { attempt_id: calls.at(-1).body.attempt_id, run_id: run.run_id, action: "approve", call_id: "call", params_hash: "original-hash" });
}));
test("真实组件：拒绝确认绑定原指纹，修改 Run ID 撤销待提交卡片", async () => fixture(async ({ page, calls }) => {
  await apply(page, "component-token"); await inspect(page);
  await page.getByRole("button", { name: "预览拒绝", exact: true }).click();
  await page.getByText(/确认登记拒绝 component-run/).waitFor();
  await page.getByLabel("Run ID", { exact: true }).fill("other-run");
  assert.equal(await page.getByRole("button", { name: "确认登记一次", exact: true }).count(), 0);
  assert.equal(calls.filter(row => row.method === "POST").length, 0);
}));
test("真实组件：JSON 回答、登记校验失败、显式执行与同 Run 完成展示", async () => fixture(async ({ page, calls, state, mutate, ledger }) => {
  const asked = [{ run_id: run.run_id, seq: 1, type: "input.requested", payload: { kind: "answer", call_id: "ask", prompt: "合成 ID", schema: { type: "object" } } }];
  state({ ...run, state: "input-required" }, asked);
  await apply(page, "component-token"); await inspect(page);
  await page.getByRole("heading", { name: "输入补充：answer", exact: true }).waitFor();
  const answer = page.locator("section.card textarea");
  await answer.fill("invalid JSON");
  await page.getByRole("button", { name: "登记回答一次", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "SyntaxError" }).waitFor();
  assert.equal(calls.filter(row => row.method === "POST").length, 0);
  mutate(route => route.fulfill({ status: 400, body: "private schema details" }));
  await answer.fill('{"bad":true}');
  await page.getByRole("button", { name: "登记回答一次", exact: true }).click();
  await page.getByText(/Runtime HTTP 400/).waitFor();
  await apply(page, "component-token"); await inspect(page);
  let row;
  mutate(async route => {
    if (new URL(route.request().url()).pathname.endsWith("/execute")) {
      state({ ...run, state: "completed" }, [{ run_id: run.run_id, seq: 3, type: "model.response", payload: { text: "synthetic done" } }]);
      row = { ...row, status: "COMMITTED", revision: 2, evidence_seq: 2 };
    } else {
      const { value, ...body } = route.request().postDataJSON();
      assert.deepEqual(value, { id: "SYNTHETIC" });
      row = { scope_id: ledgerIdentity.scope_id, ...body, value_hmac: "c".repeat(64), request_seq: 1, fingerprint: "b".repeat(64),
        status: "PREPARED", revision: 1, acknowledged: false, evidence_seq: null, rejection: null };
    }
    ledger(row); await route.fulfill({ json: row });
  });
  await answer.fill('{"id":"SYNTHETIC"}');
  await page.getByRole("button", { name: "登记回答一次", exact: true }).click();
  await page.getByRole("button", { name: "确认执行已登记回答一次", exact: true }).click();
  await page.getByRole("button", { name: "预览本人确认收口", exact: true }).waitFor();
  await inspect(page);
  await page.getByText("助手：synthetic done", { exact: true }).waitFor();
  assert.deepEqual(calls.filter(row => row.method === "POST").at(-1).body, { fingerprint: row.fingerprint, revision: 1, value: { id: "SYNTHETIC" } });
  assert.equal(await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), true);
}));
test("真实组件：创建登记结果未知，权威恢复活动锁且不自动执行", async () => fixture(async ({ page, calls, mutate, ledger }) => {
  await apply(page, "component-token");
  mutate(route => {
    const body = route.request().postDataJSON();
    ledger({ scope_id: ledgerIdentity.scope_id, attempt_id: body.attempt_id, action: "create", run_id: "run_" + "a".repeat(20), request_seq: 0,
      create_agent: body.agent_id, create_version: "1", create_model: "default-chat", create_reason: "only", create_snapshot: "c".repeat(64),
      value_hmac: "d".repeat(64), fingerprint: "b".repeat(64), status: "PREPARED", revision: 1, acknowledged: false, evidence_seq: null, rejection: null });
    return route.abort("failed");
  });
  await page.getByLabel("消息", { exact: true }).fill("synthetic");
  await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "结果未知" }).waitFor();
  assert.equal(calls.filter(row => row.method === "POST").length, 1);
  await inspect(page);
  assert.equal(await page.getByRole("button", { name: "预览批准", exact: true }).isDisabled(), true);
  await apply(page, "new-component-token");
  assert.equal(await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), true);
  await page.getByRole("button", { name: "确认执行已登记创建一次", exact: true }).waitFor();
  assert.equal(await page.getByLabel("登记时的原始创建消息", { exact: true }).inputValue(), "");
  assert.equal(calls.filter(row => row.method === "POST").length, 1);
  assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
}));
test("真实组件：凭据写前登记不执行，reconcile 卡片不提供写操作", async () => fixture(async ({ page, calls, state, mutate, ledger }) => {
  state({ ...run, state: "input-required" }, [{ run_id: run.run_id, seq: 1, type: "input.requested", payload: { kind: "credential", reason: "expiring" } }]);
  await apply(page, "component-token"); await inspect(page);
  mutate(async route => { const body = route.request().postDataJSON();
    const row = { scope_id: ledgerIdentity.scope_id, ...body, action: "credential", request_seq: 1, fingerprint: "b".repeat(64),
      status: "PREPARED", revision: 1, acknowledged: false, evidence_seq: null, rejection: null };
    ledger(row); await route.fulfill({ json: row });
  });
  const response = page.waitForResponse(value => new URL(value.url()).pathname === "/v1/workbench/attempts" && value.request().method() === "POST");
  await page.getByRole("button", { name: "登记当前用户凭据刷新", exact: true }).click();
  await response;
  await page.getByRole("button", { name: "确认执行已登记凭据刷新一次", exact: true }).waitFor();
  assert.equal(calls.filter(row => row.method === "POST").length, 1);
  assert.deepEqual(Object.keys(calls.find(row => row.method === "POST").body).sort(), ["attempt_id", "run_id"]);
  state({ ...run, state: "input-required" }, [{ run_id: run.run_id, seq: 2, type: "input.requested", payload: { kind: "reconcile" } }]);
  await page.getByRole("button", { name: "读取 Run 与待办卡片", exact: true }).click();
  await page.getByText(/未知写结果对账仅展示/).waitFor();
  assert.equal(await page.getByRole("button", { name: "登记回答一次", exact: true }).count(), 0);
}));
test("凭据组件：执行响应未知，只读核对和换令牌恢复终结回执但不能自动解锁", async () => fixture(async ({ page, calls, state, mutate, ledger }) => {
  state({ ...run, state: "input-required" }, [{ run_id: run.run_id, seq: 1, type: "input.requested", payload: { kind: "credential", reason: "expiring" } }]);
  await apply(page, "synthetic-old-credential"); await inspect(page);
  let row;
  mutate(async route => {
    if (new URL(route.request().url()).pathname.endsWith("/execute")) {
      row = { ...row, status: "COMMITTED", evidence_seq: 2, revision: 2 }; ledger(row); return route.abort("failed");
    }
    row = { scope_id: ledgerIdentity.scope_id, ...route.request().postDataJSON(), action: "credential", request_seq: 1,
      fingerprint: "b".repeat(64), status: "PREPARED", revision: 1, acknowledged: false, evidence_seq: null, rejection: null };
    ledger(row); return route.fulfill({ json: row });
  });
  await page.getByRole("button", { name: "登记当前用户凭据刷新", exact: true }).click();
  await page.getByRole("button", { name: "确认执行已登记凭据刷新一次", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "已锁定所有写操作" }).waitFor();
  await inspect(page);
  assert.equal(await page.getByRole("button", { name: "登记当前用户凭据刷新", exact: true }).isDisabled(), true);
  await apply(page, "synthetic-new-credential"); await inspect(page);
  assert.equal(await page.getByRole("button", { name: "登记当前用户凭据刷新", exact: true }).isDisabled(), true);
  assert.equal(await page.getByRole("button", { name: "确认执行已登记凭据刷新一次", exact: true }).count(), 0);
  assert.equal(await page.getByRole("button", { name: "预览本人确认收口", exact: true }).count(), 1);
  assert.equal(await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), true);
  assert.equal(calls.filter(row => row.method === "POST").length, 2);
  assert.deepEqual(calls.filter(row => row.method === "POST").at(-1).body, { fingerprint: row.fingerprint, revision: 1 });
  assert.equal(calls.at(-1).headers.authorization, "Bearer synthetic-new-credential");
}));
