import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";
const encode = source => `data:text/javascript;base64,${Buffer.from(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText).toString("base64")}`;
const operations = encode(readFileSync(new URL("../src/operations.ts", import.meta.url), "utf8"));
const source = readFileSync(new URL("../src/trusted-feedback-metrics.ts", import.meta.url), "utf8").replace('"./operations"', JSON.stringify(operations));
const { trustedFeedbackQuery, readTrustedFeedback, validateTrustedFeedback } = await import(encode(source));
const operator = { user: "ops", role: "TENANT_ADMIN", tenant: "default" };
const path = trustedFeedbackQuery("agent", "2026-10-18T00:00:00Z", "2026-10-19T00:00:00Z");
function body() { return { scope: "local_feedback_aggregate_v2", trust_mode: "runtime_observed_revision_v2", tenant_id: "default", agent_id: "agent",
  subject: "human:admin", auth_method: "user", since: "2026-10-18T00:00:00Z", until: "2026-10-19T00:00:00Z",
  read_started_at: "2026-10-19T00:00:00Z", read_finished_at: "2026-10-19T00:00:01Z", cohort_observed_at: "2026-10-19T00:00:00Z",
  complete_scan: true, snapshot_guaranteed: false, historical_snapshot_supported: false, counts: { completed: 2, failed: 1, canceled: 0, pending: 1 },
  feedback: { positive_runs: 0, negative_runs: 0, unverified_runs: 1, neutral_runs: 0, pending_runs: 0, conflicting_runs: 0,
    completed_runs: 2, verified_feedback_runs: 0, satisfaction: null, feedback_coverage: null, reason: "INCOMPLETE_PROVENANCE" } }; }
test("可信v2读取：仅GET，Bearer与开发身份范围分离，无持久化或重试", async () => {
  const calls = [], controller = new AbortController();
  const result = await readTrustedFeedback(path, operator, "isolated-admin", controller.signal, true, async (...args) => { calls.push(args); return Response.json(body()); });
  assert.equal(result.feedback.satisfaction, null); assert.equal(calls.length, 1);
  assert.equal(calls[0][1].method, "GET"); assert.equal(calls[0][1].cache, "no-store"); assert.equal(calls[0][1].redirect, "error");
  assert.equal(calls[0][1].headers.Authorization, "Bearer isolated-admin");
  await assert.rejects(readTrustedFeedback(path, operator, "token", controller.signal, false));
});
test("可信v2读取：未验证来源不能伪造可信比例，错误范围与计数拒绝", () => {
  for (const change of [row => { row.tenant_id = "other"; }, row => { row.until = "2026-10-20T00:00:00Z"; },
    row => { row.feedback.feedback_coverage = 0; }, row => { row.feedback.satisfaction = 1; }, row => { row.feedback.positive_runs = 1; },
    row => { row.counts.completed = true; }, row => { row.complete_scan = false; }, row => { row.feedback.unverified_runs = 3; }]) {
    const row = body(); change(row); assert.throws(() => validateTrustedFeedback(row, "default", path));
  }
  const empty = body(); empty.feedback.unverified_runs = 0; empty.feedback.feedback_coverage = 0; empty.feedback.reason = "NO_BINARY_FEEDBACK";
  assert.equal(validateTrustedFeedback(empty, "default", path).feedback.feedback_coverage, 0);
});
test("可信v2读取：忽略abort的旧正文迟到也不得返回结果，错误正文不回显", async () => {
  const controller = new AbortController(); let release, entered;
  const ready = new Promise(resolve => { entered = resolve; });
  const pending = readTrustedFeedback(path, operator, "isolated-admin", controller.signal, true, async () => ({ ok: true, status: 200,
    json: async () => { entered(); return new Promise(resolve => { release = resolve; }); } }));
  await ready; controller.abort(); release(body()); await assert.rejects(pending, { name: "AbortError" });
  await assert.rejects(readTrustedFeedback(path, operator, "isolated-admin", new AbortController().signal, true,
    async () => new Response("PRIVATE-SERVER-TEXT", { status: 403 })), error => !String(error).includes("PRIVATE-SERVER"));
});

test("可信v2：分子分母一致才展示，混合历史与未决一律未知", () => {
  const value = body(); Object.assign(value.feedback, { unverified_runs: 0, positive_runs: 1, neutral_runs: 1,
    verified_feedback_runs: 1, satisfaction: 1, feedback_coverage: 0.5, reason: null });
  assert.equal(validateTrustedFeedback(value, "default", path).feedback.feedback_coverage, 0.5);
  for (const change of [row => { row.feedback.pending_runs = 1; }, row => { row.feedback.unverified_runs = 1; },
    row => { row.feedback.feedback_coverage = 1; }, row => { row.feedback.neutral_runs = 2; }]) {
    const row = structuredClone(value); change(row); assert.throws(() => validateTrustedFeedback(row, "default", path));
  }
});
