import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { browser, profile } from "./support.mjs";
import { held, hold, installReadBarrier, releaseHeld } from "./readonly-fixture.mjs";
let engine;
before(async () => { engine = await browser(); });
after(async () => { await engine?.close(); });
function result(url, subject = "human:admin") {
  return { scope: "local_feedback_audit_v1", trust_mode: "strict_readonly_unverified", tenant_id: "default", agent_id: url.searchParams.get("agent_id"),
    subject, auth_method: "user", since: url.searchParams.get("since"), until: url.searchParams.get("until"), read_started_at: "2026-10-22T00:00:00Z",
    read_finished_at: "2026-10-22T00:00:01Z", cohort_observed_at: "2026-10-22T00:00:00Z", complete_scan: true, snapshot_guaranteed: false,
    historical_snapshot_supported: false, counts: { completed: 2, failed: 1, canceled: 1, pending: 1 }, feedback: {
      positive_runs: 0, negative_runs: 0, unverified_runs: 1, non_binary_runs: 0, invalid_score_runs: 0, conflicting_runs: 0,
      pre_terminal_runs: 0, completed_runs: 2, verified_feedback_runs: 0, satisfaction: null, feedback_coverage: null, reason: "UNVERIFIABLE_OR_CONFLICTING" } };
}
async function apply(page, token = "isolated-admin") {
  await page.getByLabel("反馈读取令牌（验签平台管理员）", { exact: true }).fill(token);
  await page.getByRole("button", { name: "应用反馈读取身份", exact: true }).click();
  assert.equal(await page.getByLabel("反馈读取令牌（验签平台管理员）", { exact: true }).inputValue(), "");
}
async function form(page, agent = "synthetic-agent", since = "2026-10-22T00:00", until = "2026-10-23T00:00") {
  await page.getByLabel("反馈 Agent ID", { exact: true }).fill(agent);
  await page.getByLabel("反馈开始（浏览器本地时间）", { exact: true }).fill(since);
  await page.getByLabel("反馈结束（浏览器本地时间）", { exact: true }).fill(until);
}
async function fixture(action) {
  const context = await engine.newContext({ timezoneId: "UTC" }); await installReadBarrier(context);
  const page = await context.newPage(); page.setDefaultTimeout(5000);
  const calls = []; let handler = async route => route.fulfill({ json: result(new URL(route.request().url())) });
  await page.route("**/console/metrics/feedback?**", async route => {
    calls.push({ method: route.request().method(), token: route.request().headers().authorization }); await handler(route);
  });
  try {
    await page.goto(`${profile.url}/tests/browser/management.html?view=operations`);
    await action({ page, calls, handle: value => { handler = value; } });
    assert.equal(calls.every(row => row.method === "GET"), true);
    assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
  } finally { await context.close(); }
}
test("反馈DOM：未验证满意度未知、分母与诊断分列、改窗与权限失败清空", async () => fixture(async ({ page, calls, handle }) => {
  await apply(page); await form(page);
  await page.getByRole("button", { name: "查询反馈审计", exact: true }).click();
  const panel = page.getByRole("region", { name: "反馈审计结果", exact: true });
  await panel.getByText(/实际验签身份：default \/ human:admin/).waitFor();
  assert.match(await panel.innerText(), /未知/); assert.equal(calls.length, 1);
  handle(route => { const value = result(new URL(route.request().url())); value.feedback.unverified_runs = 0;
    value.feedback.feedback_coverage = 0; value.feedback.reason = "NO_BINARY_FEEDBACK"; return route.fulfill({ json: value }); });
  await page.getByLabel("反馈 Agent ID", { exact: true }).fill("other-agent");
  assert.equal(await panel.count(), 0);
  await page.getByRole("button", { name: "查询反馈审计", exact: true }).click();
  await panel.getByText("0.00% · 0 / 2", { exact: true }).waitFor();
  handle(route => route.fulfill({ status: 403, body: "PRIVATE-ERROR" }));
  await page.getByRole("button", { name: "查询反馈审计", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "HTTP 403" }).waitFor();
  assert.equal(await panel.count(), 0); assert.equal((await page.locator("body").innerText()).includes("PRIVATE-ERROR"), false);
  await page.getByRole("combobox").selectOption("USER");
  assert.equal(await page.getByRole("button", { name: "查询反馈审计", exact: true }).isDisabled(), true);
}));
test("反馈DOM：坏租户或伪造可信比率拒绝展示，不把客户端verified当证明", async () => fixture(async ({ page, handle }) => {
  await apply(page); await form(page);
  for (const bad of [row => { row.tenant_id = "foreign"; }, row => { row.feedback.satisfaction = 1; }]) {
    handle(route => { const value = result(new URL(route.request().url())); bad(value); return route.fulfill({ json: value }); });
    await page.getByRole("button", { name: "查询反馈审计", exact: true }).click();
    await page.getByRole("alert").waitFor();
    assert.equal(await page.getByRole("region", { name: "反馈审计结果", exact: true }).count(), 0);
  }
}));
test("反馈DOM：忽略abort的旧响应不跨令牌或卸载显示", async () => fixture(async ({ page, calls, handle }) => {
  handle(route => route.fulfill({ json: result(new URL(route.request().url()), route.request().headers().authorization.endsWith("old") ? "human:old" : "human:new") }));
  await apply(page, "token-old"); await form(page);
  await page.evaluate(() => { window.syntheticTransport.holdPath = "/console/metrics/feedback"; });
  await hold(page); await page.getByRole("button", { name: "查询反馈审计", exact: true }).click(); await held(page);
  await apply(page, "token-new"); await releaseHeld(page);
  assert.equal((await page.locator("body").innerText()).includes("human:old"), false);
  await page.evaluate(() => { window.syntheticTransport.hold = false; });
  await page.getByRole("button", { name: "查询反馈审计", exact: true }).click();
  await page.getByText(/实际验签身份：default \/ human:new/).waitFor();
  await hold(page); await page.getByRole("button", { name: "查询反馈审计", exact: true }).click(); await held(page);
  await page.locator("#unmount").click(); await releaseHeld(page);
  await page.locator("#remount").click();
  assert.equal(await page.getByRole("region", { name: "反馈审计结果", exact: true }).count(), 0);
  assert.equal(calls.length, 3);
}));
test("隔离真实反馈读取：实际Console／Runtime验签分母＋只读Langfuse替身，无证明评分仍未知", async () => {
  const tokens = JSON.parse(await readFile(profile.tokens, "utf8")), info = JSON.parse(process.env.S12_FEEDBACK_FIXTURE);
  const context = await engine.newContext({ timezoneId: "UTC" }), page = await context.newPage(); page.setDefaultTimeout(5000);
  const methods = [];
  page.on("request", request => { if (new URL(request.url()).pathname.startsWith("/console/")) methods.push(request.method()); });
  try {
    await page.goto(`${profile.url}/tests/browser/management.html?view=operations`);
    await apply(page, tokens.alice); await form(page, info.agent, info.since.slice(0, 16), info.until.slice(0, 16));
    await page.getByRole("button", { name: "查询反馈审计", exact: true }).click();
    await page.getByRole("alert").filter({ hasText: "HTTP 403" }).waitFor();
    await apply(page, tokens.feedback_admin);
    const received = page.waitForResponse(response => new URL(response.url()).pathname === "/console/metrics/feedback");
    await page.getByRole("button", { name: "查询反馈审计", exact: true }).click();
    const response = await received; assert.equal(response.status(), 200);
    const value = await response.json(); assert.equal(value.counts.completed, 1);
    assert.equal(value.feedback.unverified_runs, 1); assert.equal(value.feedback.satisfaction, null); assert.equal(value.feedback.feedback_coverage, null);
    assert.equal(value.subject, "human:feedback-reader");
    await page.getByRole("region", { name: "反馈审计结果", exact: true }).getByText(/human:feedback-reader/).waitFor();
    assert.equal((await page.locator("body").innerText()).includes("PRIVATE-"), false);
    await page.getByLabel("租户", { exact: true }).fill("foreign-feedback");
    assert.equal(await page.getByRole("region", { name: "反馈审计结果", exact: true }).count(), 0);
    assert.equal(await page.getByRole("button", { name: "查询反馈审计", exact: true }).isDisabled(), true);
    assert.equal(methods.every(method => method === "GET"), true);
    assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
  } finally { await context.close(); }
});
