import assert from "node:assert/strict";
import { profile } from "./support.mjs";
export const run = { run_id: "synthetic-run", agent_id: "synthetic-agent", agent_version: "1", subject: "human:alice", state: "completed", last_seq: 3, created_at: "2026-10-21T00:00:00Z" };
export const traceId = "1234567890abcdef1234567890abcdef";
export function event(seq, type = "model.response", payload = { text: "synthetic" }) {
  return { run_id: run.run_id, seq, type, ts: "2026-10-21T00:00:00Z", payload };
}
export function control(tenant = "default", agents = 7) {
  return { tenant_id: tenant, scope: "console_control_plane", generated_at: "2026-10-21T00:00:00Z", agents, versions: { published: 2 }, pre_release_versions: 0,
    assignment_relations: 3, assigned_principals: 2, agent_binding_records: 1, commands: { pending: 1 }, audit_events: 5 };
}
export function traces() {
  return { tenant_id: "default", agent_id: "synthetic-agent", scope: "local_langfuse_v3_trace_generations", since: "2026-10-21T00:00:00Z", until: "2026-10-21T01:00:00Z",
    generated_at: "2026-10-21T01:00:01Z", trace_calls: 3, generations: 4, complete_scan: true, snapshot_guaranteed: false, run_metrics_available: false,
    tokens: { total: null, known_trace_subtotal: 123, traces_with_complete_usage: 2 },
    cost_usd: { total: null, known_trace_subtotal: "0.000000123456789", traces_with_complete_cost: 1 } };
}
export async function installReadBarrier(context) {
  await context.addInitScript(() => {
    const original = window.fetch;
    window.syntheticTransport = { hold: false, pending: [], paths: [] };
    window.fetch = async (input, options) => {
      const state = window.syntheticTransport;
      const path = new URL(String(input), location.href).pathname;
      if (!path.startsWith("/console/") && !path.startsWith("/v1/")) return original(input, options);
      state.paths.push(path);
      const held = state.hold && (!state.holdPath || state.holdPath === path);
      const response = await original(input, { ...options, signal: undefined });
      if (!held || path.endsWith("/stream")) return response;
      const data = await response.json();
      let release;
      const barrier = new Promise(resolve => { release = resolve; });
      state.pending.push(release);
      return { ok: response.ok, status: response.status, json: async () => { await barrier; return data; } };
    };
  });
}
export async function mountReadOnly(engine, view, action) {
  const context = await engine.newContext({ timezoneId: "Asia/Shanghai" });
  await installReadBarrier(context);
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  const calls = [];
  let handler = async () => false;
  for (const pattern of ["**/console/**", "**/v1/**"]) await context.route(pattern, async route => {
    const request = route.request(), url = new URL(request.url());
    const row = { url, path: url.pathname, method: request.method(), headers: request.headers() };
    calls.push(row);
    if (await handler(route, row)) return;
    await route.fulfill({ status: 404, json: { detail: "Unconfigured isolated readonly request" } });
  });
  try {
    await page.goto(`${profile.url}/tests/browser/management.html?view=${view}`);
    await page.getByRole("heading", { name: view === "operations" ? "运营只读视图" : "Run 只读调试轨迹", exact: true }).waitFor();
    await action({ page, context, calls, handle: value => { handler = value; } });
    assert.equal(calls.every(row => row.method === "GET"), true, "Readonly components must never issue mutations");
    assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
  } finally { await context.close(); }
}
export async function identity(page, value = "synthetic-token") {
  await page.getByLabel("访问令牌", { exact: true }).fill(value);
  await page.getByRole("button", { name: "应用读取身份并清空旧数据", exact: true }).click();
  assert.equal(await page.getByLabel("访问令牌", { exact: true }).inputValue(), "");
}
export async function inspect(page) {
  await page.getByLabel("Run ID", { exact: true }).fill(run.run_id);
  await page.getByRole("button", { name: "读取指定 Run", exact: true }).click();
}
export async function traceForm(page) {
  await page.getByLabel("Agent ID", { exact: true }).fill("synthetic-agent");
  await page.getByLabel("开始（浏览器本地时间）", { exact: true }).fill("2026-10-21T08:00");
  await page.getByLabel("结束（浏览器本地时间）", { exact: true }).fill("2026-10-21T09:00");
}
export async function hold(page) { await page.evaluate(() => { window.syntheticTransport.hold = true; }); }
export async function held(page) { await page.waitForFunction(() => window.syntheticTransport.pending.length === 1); }
export async function releaseHeld(page) {
  await page.evaluate(async () => {
    window.syntheticTransport.hold = false;
    window.syntheticTransport.pending.splice(0).forEach(release => release());
    await new Promise(resolve => requestAnimationFrame(resolve));
  });
}
