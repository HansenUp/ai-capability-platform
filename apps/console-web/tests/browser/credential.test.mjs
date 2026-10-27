import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { apply, browser, phase, profile } from "./support.mjs";
const runs = JSON.parse(process.env.S12_CREDENTIAL_RUNS || "null");
assert.equal(Array.isArray(runs) && runs.length === 2, true);
async function fixture(index, action) {
  const tokens = JSON.parse(await readFile(profile.tokens, "utf8"));
  const engine = await browser(), context = await engine.newContext(), page = await context.newPage();
  page.setDefaultTimeout(5000);
  const mutations = [];
  page.on("request", request => {
    const path = new URL(request.url()).pathname;
    if (request.method() !== "GET" && path.startsWith("/v1/")) mutations.push({ path, body: request.postDataJSON(),
      alice: [tokens.alice, tokens.alice_fresh].some(token => request.headers().authorization === `Bearer ${token}`) });
  });
  try {
    await page.goto(`${profile.url}/tests/browser/workbench.html`);
    await action({ page, tokens, runId: runs[index], mutations });
    assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
    assert.equal(await page.getByLabel("用户访问令牌", { exact: true }).inputValue(), "");
  } finally { await context.close(); await engine.close(); }
}
async function locate(page, runId) {
  await page.getByLabel("Run ID", { exact: true }).fill(runId);
  await page.getByRole("button", { name: "读取 Run 与待办卡片", exact: true }).click();
}
async function history(page, runId, token) {
  const response = await page.request.get(`${profile.runtime}/v1/runs/${runId}/events`, { headers: { Authorization: `Bearer ${token}` } });
  assert.equal(response.status(), 200); return response.json();
}
function verifyHistory(events, tokens) {
  assert.equal(events.filter(row => row.type === "run.created").length, 1);
  assert.equal(events.filter(row => row.type === "input.requested" && row.payload.kind === "credential").length, 1);
  assert.equal(events.filter(row => row.type === "input.provided" && row.payload.kind === "credential").length, 1);
  assert.equal(events.some(row => row.type === "tool.executing"), false);
  assert.equal(events.some(row => row.type === "model.response" && row.payload.text === "S12 同发起人凭据刷新完成：同 Run，无外部效果。"), true);
  for (const token of Object.values(tokens)) assert.equal(JSON.stringify(events).includes(token), false);
}
async function prepare(page) {
  const accepted = page.waitForResponse(response => new URL(response.url()).pathname === "/v1/workbench/attempts" && response.request().method() === "POST");
  await page.getByRole("button", { name: "登记当前用户凭据刷新", exact: true }).click();
  const response = await accepted; assert.equal(response.status(), 200);
  await page.getByRole("button", { name: "确认执行已登记凭据刷新一次", exact: true }).waitFor();
  return response.json();
}
async function acknowledge(page) {
  await page.getByRole("button", { name: "预览本人确认收口", exact: true }).click();
  await page.getByRole("button", { name: "取消收口", exact: true }).click();
  await page.getByRole("button", { name: "预览本人确认收口", exact: true }).click();
  const accepted = page.waitForResponse(response => new URL(response.url()).pathname.endsWith("/acknowledge"));
  await page.getByRole("button", { name: "确认收口此回执", exact: true }).click();
  const response = await accepted; assert.equal(response.status(), 200);
  assert.equal((await response.json()).acknowledged, true);
  await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).waitFor({ state: "visible" });
  await page.waitForFunction(() => ![...document.querySelectorAll("button")].find(button => button.textContent === "登记新 Run（尚未创建）")?.disabled);
}
test("真实隔离凭据：过期身份401、他人403／跨租户404，原发起人新令牌恢复同Run", async () => fixture(0, async ({ page, tokens, runId, mutations }) => {
  await apply(page, tokens.expired_alice); await locate(page, runId);
  await page.getByRole("status").filter({ hasText: "Runtime HTTP 401" }).waitFor();
  assert.equal(await page.getByRole("heading", { name: "输入补充：credential", exact: true }).count(), 0);
  await apply(page, tokens.bob); await locate(page, runId);
  await page.getByRole("status").filter({ hasText: "Runtime HTTP 403" }).waitFor();
  for (const [token, status] of [[tokens.expired_alice, 401], [tokens.bob, 403], [tokens.foreign, 404]]) {
    const response = await page.request.post(`${profile.runtime}/v1/runs/${runId}/input`, {
      headers: { Authorization: `Bearer ${token}` }, data: { kind: "credential" },
    });
    assert.equal(response.status(), status);
  }
  await apply(page, tokens.alice);
  assert.equal(await page.getByLabel("Run ID", { exact: true }).inputValue(), "");
  await locate(page, runId);
  await page.getByRole("heading", { name: "输入补充：credential", exact: true }).waitFor();
  const prepared = await prepare(page);
  const accepted = page.waitForResponse(response => new URL(response.url()).pathname.endsWith("/execute"));
  await page.getByRole("button", { name: "确认执行已登记凭据刷新一次", exact: true }).click();
  assert.equal((await accepted).status(), 200);
  await phase(page, runId, "completed", tokens.alice);
  await page.getByText("助手：S12 同发起人凭据刷新完成：同 Run，无外部效果。", { exact: true }).waitFor();
  assert.equal(await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), true);
  await acknowledge(page);
  assert.deepEqual(mutations.map(row => row.path), ["/v1/workbench/attempts", `/v1/workbench/attempts/${prepared.attempt_id}/execute`, `/v1/workbench/attempts/${prepared.attempt_id}/acknowledge`]);
  assert.equal(mutations.every(row => row.alice), true);
  assert.deepEqual(mutations[0].body, { run_id: runId, attempt_id: prepared.attempt_id });
  assert.deepEqual(mutations[1].body, { fingerprint: prepared.fingerprint, revision: 1 });
  assert.deepEqual(mutations[2].body, { fingerprint: prepared.fingerprint, revision: 2 });
  verifyHistory(await history(page, runId, tokens.alice), tokens);
}));
test("真实持久账本：准备后重载不执行、已提交响应丢失后跨页恢复，原操作者显式确认才收口", async () => fixture(1, async ({ page, tokens, runId, mutations }) => {
  await apply(page, tokens.alice); await locate(page, runId);
  await page.getByRole("heading", { name: "输入补充：credential", exact: true }).waitFor();
  const prepared = await prepare(page);
  await page.reload(); await apply(page, tokens.alice);
  await page.getByRole("button", { name: "确认执行已登记凭据刷新一次", exact: true }).waitFor();
  assert.equal(mutations.length, 1);
  assert.equal((await history(page, runId, tokens.alice)).filter(row => row.type === "input.provided" && row.payload.kind === "credential").length, 0);
  const second = await page.context().newPage();
  const competingWrites = [];
  second.on("request", request => { if (request.method() === "POST") competingWrites.push({ path: new URL(request.url()).pathname, body: request.postDataJSON() }); });
  await second.goto(`${profile.url}/tests/browser/workbench.html`); await apply(second, tokens.alice);
  assert.equal(await second.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), true);
  await second.getByRole("button", { name: "确认执行已登记凭据刷新一次", exact: true }).waitFor();
  let committed = false;
  await page.route(`**/v1/workbench/attempts/${prepared.attempt_id}/execute`, async route => {
    const response = await route.fetch();
    assert.equal(response.status(), 200); committed = true;
    await route.abort("failed");
  });
  await Promise.all([page.getByRole("button", { name: "确认执行已登记凭据刷新一次", exact: true }).click(),
    second.getByRole("button", { name: "确认执行已登记凭据刷新一次", exact: true }).click()]);
  await page.getByRole("status").filter({ hasText: "结果未知" }).waitFor();
  await page.getByRole("alert").filter({ hasText: "已锁定所有写操作" }).waitFor();
  assert.equal(committed, true);
  await second.getByRole("button", { name: "预览本人确认收口", exact: true }).waitFor();
  await page.context().setOffline(true);
  await second.getByRole("button", { name: "只读核对写账本", exact: true }).click();
  await second.getByRole("status").filter({ hasText: "读取失败" }).waitFor();
  assert.equal(await second.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), true);
  await page.context().setOffline(false);
  await second.getByRole("button", { name: "只读核对写账本", exact: true }).click();
  await second.getByRole("button", { name: "预览本人确认收口", exact: true }).waitFor();
  assert.equal(await second.getByRole("button", { name: "确认执行已登记凭据刷新一次", exact: true }).count(), 0);
  assert.deepEqual(competingWrites, [{ path: `/v1/workbench/attempts/${prepared.attempt_id}/execute`, body: { fingerprint: prepared.fingerprint, revision: 1 } }]);
  await second.close();
  await page.evaluate(() => { localStorage.clear(); sessionStorage.clear(); });
  assert.notEqual(tokens.alice, tokens.alice_fresh);
  await page.reload(); await apply(page, tokens.alice_fresh); await locate(page, runId);
  const refreshedIdentity = await page.request.get(`${profile.runtime}/v1/workbench/identity`, { headers: { Authorization: `Bearer ${tokens.alice_fresh}` } });
  assert.equal((await refreshedIdentity.json()).scope_id, prepared.scope_id);
  await page.getByRole("button", { name: "预览本人确认收口", exact: true }).waitFor();
  await phase(page, runId, "completed", tokens.alice);
  assert.equal(await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), true);
  await page.getByRole("button", { name: "跟随／续读已持久化事件", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "不推断 Run 完成" }).waitFor();
  await apply(page, tokens.bob); await locate(page, runId);
  await page.getByRole("status").filter({ hasText: "Runtime HTTP 403" }).waitFor();
  assert.equal(await page.getByRole("region", { name: "活动写回执", exact: true }).count(), 0);
  for (const token of [tokens.bob, tokens.foreign]) {
    const response = await page.request.post(`${profile.runtime}/v1/workbench/attempts/${prepared.attempt_id}/acknowledge`, {
      headers: { Authorization: `Bearer ${token}` }, data: { fingerprint: prepared.fingerprint, revision: 2 },
    });
    assert.equal(response.status(), 404);
  }
  await apply(page, tokens.alice_fresh); await locate(page, runId);
  await page.getByRole("heading", { name: new RegExp(`${runId} · completed`) }).waitFor();
  assert.equal(await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), true);
  await page.getByRole("alert").filter({ hasText: "已锁定所有写操作" }).waitFor();
  assert.equal(mutations.length, 2);
  for (const [path, body] of [["/v1/runs", { agent_id: "demo", input: [{ role: "user", content: "must not run" }] }],
    [`/v1/runs/${runId}/input`, { kind: "credential" }]]) {
    const response = await page.request.post(`${profile.runtime}${path}`, { headers: { Authorization: `Bearer ${tokens.alice}` }, data: body });
    assert.equal(response.status(), 409);
  }
  await acknowledge(page);
  assert.deepEqual(mutations.map(row => row.path), ["/v1/workbench/attempts", `/v1/workbench/attempts/${prepared.attempt_id}/execute`, `/v1/workbench/attempts/${prepared.attempt_id}/acknowledge`]);
  verifyHistory(await history(page, runId, tokens.alice), tokens);
}));
