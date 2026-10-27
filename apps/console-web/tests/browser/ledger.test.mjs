import { test } from "node:test";
import assert from "node:assert/strict";
import { apply, browser, profile, ledgerIdentity } from "./support.mjs";
const row = { scope_id: ledgerIdentity.scope_id, attempt_id: "11111111-1111-4111-8111-111111111111", run_id: "run1",
  action: "credential", request_seq: 4, fingerprint: "b".repeat(64), status: "COMMITTED", revision: 2,
  evidence_seq: 5, rejection: null, acknowledged: false };
async function fixture(action) {
  const engine = await browser(), context = await engine.newContext(), page = await context.newPage();
  page.setDefaultTimeout(5000);
  const calls = []; let active = row;
  await context.route("**/v1/**", async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    calls.push([request.method(), path, request.headers().authorization]);
    if (request.method() !== "GET") return route.fulfill({ status: 405 });
    if (path.endsWith("/identity")) return route.fulfill({ json: ledgerIdentity });
    if (path.endsWith("/attempts")) return route.fulfill({ json: { scope_id: ledgerIdentity.scope_id, active } });
    if (path === "/v1/runs/run1") return route.fulfill({ json: { run_id: "run1", agent_id: "demo", subject: "human:alice", state: "input-required", last_seq: 4 } });
    if (path === "/v1/runs/run1/events") return route.fulfill({ json: [{ run_id: "run1", seq: 4, type: "input.requested", payload: { kind: "credential", reason: "expiring" } }] });
    return route.fulfill({ status: 404 });
  });
  try { await action({ context, page, calls, setActive: value => { active = value; } }); }
  finally { await context.close(); await engine.close(); }
}
test("账本 DOM：空列表、同ID错Run、指纹或版本倒退不能解除已知活动锁", async () => fixture(async ({ page, calls, setActive }) => {
  await page.goto(`${profile.url}/tests/browser/workbench.html`); await apply(page, "synthetic-owner");
  await page.getByRole("button", { name: "预览本人确认收口", exact: true }).waitFor();
  for (const invalid of [null, { ...row, run_id: "other" }, { ...row, fingerprint: "c".repeat(64) },
    { ...row, revision: 1 }, { ...row, status: "PREPARED", revision: 3, evidence_seq: null }]) {
    setActive(invalid);
    await page.getByRole("button", { name: "只读核对写账本", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "保持写锁" }).waitFor();
    assert.equal(await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), true);
    assert.equal(await page.getByRole("button", { name: "预览本人确认收口", exact: true }).isDisabled(), true);
  }
  assert.equal(calls.every(([method]) => method === "GET"), true);
}));
test("账本 DOM：登记响应丢失后只读恢复，空列表不能解开已知attempt，也不自动执行", async () => fixture(async ({ page, setActive }) => {
  setActive(null); let prepared, writes = 0;
  await page.route("**/v1/workbench/attempts", async route => {
    if (route.request().method() === "GET") return route.fallback();
    writes++;
    prepared = { ...row, ...route.request().postDataJSON(), status: "PREPARED", revision: 1, evidence_seq: null };
    setActive(prepared); await route.abort("failed");
  });
  await page.goto(`${profile.url}/tests/browser/workbench.html`); await apply(page, "synthetic-owner");
  await page.getByLabel("Run ID", { exact: true }).fill("run1");
  await page.getByRole("button", { name: "读取 Run 与待办卡片", exact: true }).click();
  await page.getByRole("button", { name: "登记当前用户凭据刷新", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "结果未知" }).waitFor();
  setActive(null);
  await page.getByRole("button", { name: "只读核对写账本", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "账本与已知未决记录冲突" }).waitFor();
  assert.equal(await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), true);
  setActive(prepared);
  await page.getByRole("button", { name: "只读核对写账本", exact: true }).click();
  await page.getByRole("button", { name: "确认执行已登记凭据刷新一次", exact: true }).waitFor();
  assert.equal(writes, 1);
  await page.reload(); await apply(page, "synthetic-owner");
  await page.getByRole("button", { name: "确认执行已登记凭据刷新一次", exact: true }).waitFor();
  assert.equal(writes, 1);
}));
test("账本 DOM：身份读取未完成时禁写，忽略abort的旧身份响应不继续账本查询或污染新主体", async () => fixture(async ({ context, page, calls, setActive }) => {
  setActive(null);
  await context.addInitScript(identity => {
    const native = window.fetch.bind(window);
    window.oldLedger = { held: false, release: null };
    window.fetch = async (path, options = {}) => {
      if (String(path) === "/v1/workbench/identity" && options.headers?.Authorization === "Bearer old-synthetic") {
        window.oldLedger.held = true;
        return new Promise(resolve => { window.oldLedger.release = () => resolve(Response.json({ ...identity, scope_id: "d".repeat(64), subject: "human:old" })); });
      }
      return native(path, options);
    };
  }, ledgerIdentity);
  await page.goto(`${profile.url}/tests/browser/workbench.html`);
  await page.getByLabel("用户访问令牌", { exact: true }).fill("old-synthetic");
  await page.getByRole("button", { name: "应用身份并清空旧数据", exact: true }).click();
  await page.waitForFunction(() => window.oldLedger.held);
  assert.equal(await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), true);
  await apply(page, "new-synthetic");
  await page.evaluate(() => window.oldLedger.release());
  await page.getByText(`${ledgerIdentity.tenant_id} / ${ledgerIdentity.subject} · ${ledgerIdentity.scope_id}`, { exact: true }).waitFor();
  assert.equal(await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), false);
  assert.equal(calls.filter(([, path]) => path.endsWith("/attempts")).length, 1);
  assert.equal(calls.every(([, , token]) => token === "Bearer new-synthetic"), true);
  assert.equal((await page.locator("body").innerText()).includes("human:old"), false);
}));
