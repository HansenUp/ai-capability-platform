import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import { browser, profile } from "./support.mjs";
import { held, hold, installReadBarrier, releaseHeld } from "./readonly-fixture.mjs";
let engine;
before(async () => { engine = await browser(); });
after(async () => { await engine?.close(); });
function result(url, subject = "human:admin") {
  return { scope: "local_feedback_aggregate_v2", trust_mode: "runtime_observed_revision_v2", tenant_id: "default", agent_id: url.searchParams.get("agent_id"),
    subject, auth_method: "user", since: url.searchParams.get("since"), until: url.searchParams.get("until"), read_started_at: "2026-10-22T00:00:00Z",
    read_finished_at: "2026-10-22T00:00:01Z", cohort_observed_at: "2026-10-22T00:00:00Z", complete_scan: true, snapshot_guaranteed: false,
    historical_snapshot_supported: false, counts: { completed: 2, failed: 1, canceled: 1, pending: 1 }, feedback: {
      positive_runs: 0, negative_runs: 0, unverified_runs: 1, neutral_runs: 0, pending_runs: 0, conflicting_runs: 0,
      completed_runs: 2, verified_feedback_runs: 0, satisfaction: null, feedback_coverage: null, reason: "INCOMPLETE_PROVENANCE" } };
}
async function apply(page, token = "isolated-admin") {
  await page.getByLabel("v2 读取令牌（验签平台管理员）", { exact: true }).fill(token);
  await page.getByRole("button", { name: "应用 v2 读取身份", exact: true }).click();
  assert.equal(await page.getByLabel("v2 读取令牌（验签平台管理员）", { exact: true }).inputValue(), "");
}
async function form(page, agent = "synthetic-agent", since = "2026-10-22T00:00", until = "2026-10-23T00:00") {
  await page.getByLabel("v2 Agent ID", { exact: true }).fill(agent);
  await page.getByLabel("v2 开始（浏览器本地时间）", { exact: true }).fill(since);
  await page.getByLabel("v2 结束（浏览器本地时间）", { exact: true }).fill(until);
}
async function fixture(action) {
  const context = await engine.newContext({ timezoneId: "UTC" }); await installReadBarrier(context);
  const page = await context.newPage(); page.setDefaultTimeout(5000);
  const calls = []; let handler = async route => route.fulfill({ json: result(new URL(route.request().url())) });
  await page.route("**/console/metrics/feedback-v2?**", async route => {
    calls.push({ method: route.request().method(), token: route.request().headers().authorization }); await handler(route);
  });
  try {
    await page.goto(`${profile.url}/tests/browser/management.html?view=operations`);
    await action({ page, calls, handle: value => { handler = value; } });
    assert.equal(calls.every(row => row.method === "GET"), true);
    assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
  } finally { await context.close(); }
}
test("可信聚合v2 DOM：未验证满意度未知、分母与诊断分列、改窗与权限失败清空", async () => fixture(async ({ page, calls, handle }) => {
  await apply(page); await form(page);
  await page.getByRole("button", { name: "查询可信反馈 v2", exact: true }).click();
  const panel = page.getByRole("region", { name: "可信反馈 v2 结果", exact: true });
  await panel.getByText(/实际验签身份：default \/ human:admin/).waitFor();
  assert.match(await panel.innerText(), /未知/); assert.equal(calls.length, 1);
  handle(route => { const value = result(new URL(route.request().url())); value.feedback.unverified_runs = 0;
    value.feedback.feedback_coverage = 0; value.feedback.reason = "NO_BINARY_FEEDBACK"; return route.fulfill({ json: value }); });
  await page.getByLabel("v2 Agent ID", { exact: true }).fill("other-agent");
  assert.equal(await panel.count(), 0);
  await page.getByRole("button", { name: "查询可信反馈 v2", exact: true }).click();
  await panel.getByText("0.00% · 0 / 2", { exact: true }).waitFor();
  handle(route => route.fulfill({ status: 403, body: "PRIVATE-ERROR" }));
  await page.getByRole("button", { name: "查询可信反馈 v2", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "HTTP 403" }).waitFor();
  assert.equal(await panel.count(), 0); assert.equal((await page.locator("body").innerText()).includes("PRIVATE-ERROR"), false);
  await page.getByRole("combobox").selectOption("USER");
  assert.equal(await page.getByRole("button", { name: "查询可信反馈 v2", exact: true }).isDisabled(), true);
}));
test("可信聚合v2 DOM：坏租户或伪造可信比率拒绝展示，不把客户端verified当证明", async () => fixture(async ({ page, handle }) => {
  await apply(page); await form(page);
  for (const bad of [row => { row.tenant_id = "foreign"; }, row => { row.feedback.satisfaction = 1; }]) {
    handle(route => { const value = result(new URL(route.request().url())); bad(value); return route.fulfill({ json: value }); });
    await page.getByRole("button", { name: "查询可信反馈 v2", exact: true }).click();
    await page.getByRole("alert").waitFor();
    assert.equal(await page.getByRole("region", { name: "可信反馈 v2 结果", exact: true }).count(), 0);
  }
}));
test("可信聚合v2 DOM：忽略abort的旧响应不跨令牌或卸载显示", async () => fixture(async ({ page, calls, handle }) => {
  handle(route => route.fulfill({ json: result(new URL(route.request().url()), route.request().headers().authorization.endsWith("old") ? "human:old" : "human:new") }));
  await apply(page, "token-old"); await form(page);
  await page.evaluate(() => { window.syntheticTransport.holdPath = "/console/metrics/feedback-v2"; });
  await hold(page); await page.getByRole("button", { name: "查询可信反馈 v2", exact: true }).click(); await held(page);
  await apply(page, "token-new"); await releaseHeld(page);
  assert.equal((await page.locator("body").innerText()).includes("human:old"), false);
  await page.evaluate(() => { window.syntheticTransport.hold = false; });
  await page.getByRole("button", { name: "查询可信反馈 v2", exact: true }).click();
  await page.getByText(/实际验签身份：default \/ human:new/).waitFor();
  await hold(page); await page.getByRole("button", { name: "查询可信反馈 v2", exact: true }).click(); await held(page);
  await page.locator("#unmount").click(); await releaseHeld(page);
  await page.locator("#remount").click();
  assert.equal(await page.getByRole("region", { name: "可信反馈 v2 结果", exact: true }).count(), 0);
  assert.equal(calls.length, 3);
}));
test("隔离真实v2反馈读取：实际Console／Runtime验签分母＋只读Langfuse替身，无证明评分仍未知", async () => {
  const tokens = JSON.parse(await readFile(profile.tokens, "utf8")), info = JSON.parse(process.env.S12_FEEDBACK_FIXTURE);
  const context = await engine.newContext({ timezoneId: "UTC" }), page = await context.newPage(); page.setDefaultTimeout(5000);
  const methods = [];
  page.on("request", request => { if (new URL(request.url()).pathname.startsWith("/console/")) methods.push(request.method()); });
  try {
    await page.goto(`${profile.url}/tests/browser/management.html?view=operations`);
    await apply(page, tokens.alice); await form(page, info.agent, info.since.slice(0, 16), info.until.slice(0, 16));
    await page.getByRole("button", { name: "查询可信反馈 v2", exact: true }).click();
    await page.getByRole("alert").filter({ hasText: "HTTP 403" }).waitFor();
    await apply(page, tokens.feedback_admin);
    const received = page.waitForResponse(response => new URL(response.url()).pathname === "/console/metrics/feedback-v2");
    await page.getByRole("button", { name: "查询可信反馈 v2", exact: true }).click();
    const response = await received; assert.equal(response.status(), 200);
    const value = await response.json(); assert.equal(value.counts.completed, 1);
    assert.equal(value.feedback.unverified_runs, 1); assert.equal(value.feedback.satisfaction, null); assert.equal(value.feedback.feedback_coverage, null);
    assert.equal(value.subject, "human:feedback-reader");
    await page.getByRole("region", { name: "可信反馈 v2 结果", exact: true }).getByText(/human:feedback-reader/).waitFor();
    assert.equal((await page.locator("body").innerText()).includes("PRIVATE-"), false);
    await page.getByLabel("租户", { exact: true }).fill("foreign-feedback");
    assert.equal(await page.getByRole("region", { name: "可信反馈 v2 结果", exact: true }).count(), 0);
    assert.equal(await page.getByRole("button", { name: "查询可信反馈 v2", exact: true }).isDisabled(), true);
    assert.equal(methods.every(method => method === "GET"), true);
    assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
  } finally { await context.close(); }
});

test("可信聚合v2 DOM：已证明二元分子分母与中性独立展示", async () => fixture(async ({ page, handle }) => {
  handle(route => { const value = result(new URL(route.request().url()));
    Object.assign(value.feedback, { unverified_runs: 0, positive_runs: 1, neutral_runs: 1, verified_feedback_runs: 1,
      satisfaction: 1, feedback_coverage: 0.5, reason: null }); return route.fulfill({ json: value }); });
  await apply(page); await form(page);
  await page.getByRole("button", { name: "查询可信反馈 v2", exact: true }).click();
  const panel = page.getByRole("region", { name: "可信反馈 v2 结果", exact: true });
  await panel.getByText("50.00% · 1 / 2", { exact: true }).waitFor();
  await panel.getByText("100.00%", { exact: true }).waitFor();
}));

test("隔离真实v2：权威OBSERVED、最新未发送不回溯、中性修订与独立Run隔离", async () => {
  const tokens = JSON.parse(await readFile(profile.tokens, "utf8")), info = JSON.parse(process.env.S12_FEEDBACK_FIXTURE);
  Object.assign(info, info.v2);
  const context = await engine.newContext({ timezoneId: "UTC" }), page = await context.newPage(); page.setDefaultTimeout(5000);
  const root = process.env.S12_RUNTIME_URL, headers = { Authorization: `Bearer ${tokens.alice}` };
  async function prepare(value, expected) {
    const response = await context.request.post(`${root}/v1/workbench/feedback-attempts`, { headers,
      data: { attempt_id: randomUUID(), run_id: info.run_id, expected_revision: expected, score: value } });
    assert.equal(response.status(), 200, await response.text()); return response.json();
  }
  async function action(row, name) {
    const response = await context.request.post(`${root}/v1/workbench/feedback-attempts/${row.attempt_id}/${name}`, {
      headers, data: { fingerprint: row.fingerprint, revision: row.revision } });
    assert.equal(response.status(), 200, await response.text()); return response.json();
  }
  async function query() {
    const received = page.waitForResponse(response => new URL(response.url()).pathname === "/console/metrics/feedback-v2");
    await page.getByRole("button", { name: "查询可信反馈 v2", exact: true }).click();
    const response = await received; assert.equal(response.status(), 200, await response.text()); return response.json();
  }
  try {
    await page.goto(`${profile.url}/tests/browser/management.html?view=operations`);
    await apply(page, tokens.feedback_admin); await form(page, info.agent, info.since.slice(0, 16), info.until.slice(0, 16));
    const first = await prepare(1, 0), sent = await action(first, "execute"), observed = await action(sent, "reconcile");
    assert.equal(observed.status, "OBSERVED");
    let value = await query();
    assert.equal(value.feedback.positive_runs, 1); assert.equal(value.feedback.unverified_runs, 0);
    assert.equal(value.feedback.satisfaction, 1); assert.equal(value.feedback.feedback_coverage, 1);
    await action(observed, "acknowledge");
    const neutral = await prepare(0, 1);
    value = await query(); assert.equal(value.feedback.pending_runs, 1); assert.equal(value.feedback.positive_runs, 0);
    const neutralObserved = await action(await action(neutral, "execute"), "reconcile");
    value = await query(); assert.equal(value.feedback.neutral_runs, 1); assert.equal(value.feedback.positive_runs, 0);
    assert.equal(value.feedback.satisfaction, null);
    await action(neutralObserved, "acknowledge");
    await page.getByRole("region", { name: "可信反馈 v2 结果", exact: true }).getByText(/human:feedback-reader/).waitFor();
    assert.equal((await page.locator("body").innerText()).includes("PRIVATE-"), false);
  } finally { await context.close(); }
});
