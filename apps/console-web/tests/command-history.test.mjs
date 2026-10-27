import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";
const source = readFileSync(new URL("../src/command-history.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
const history = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`);
test("历史走操作者专用接口，编码节点与过滤条件", () => {
  const path = history.historyPath("n/a", "pending", "reload_config", 100);
  const url = new URL(path, "http://localhost");
  assert.equal(url.pathname, "/nodes/n%2Fa/command-history");
  assert.equal(url.searchParams.get("before_id"), "100");
  assert.equal(url.searchParams.get("status"), "pending");
  assert.throws(() => history.historyPath("", "", "", null));
});
test("审计固定 node.command、原节点与操作者，不传可覆盖租户", () => {
  const url = new URL(history.auditPath("node", "original", "2026-10-21T08:00:00+08:00", "", null), "http://localhost");
  assert.equal(url.searchParams.get("action"), "node.command");
  assert.equal(url.searchParams.get("object"), "node");
  assert.equal(url.searchParams.get("actor"), "original");
  assert.equal(url.searchParams.get("since"), "2026-10-21T00:00:00.000Z");
  assert.equal(url.searchParams.has("tenant"), false);
  assert.throws(() => history.auditPath("node", "", "2026-10-21T08:00:00", "", null));
  assert.throws(() => history.auditPath("node", "", "2026-10-22T00:00:00Z", "2026-10-21T00:00:00Z", null));
});
test("游标递减且必须是可安全表示的正整数", () => {
  assert.equal(history.nextCursor(10, 20), 10); assert.equal(history.nextCursor(null, 10), null);
  for (const value of [0, -1, 20, 21, NaN, Number.MAX_SAFE_INTEGER + 1, "1"]) assert.throws(() => history.nextCursor(value, 20));
});
const row = { id: 1, tenant: "t", object: "node", actor: "ops", action: "node.command", detail: { id: "cmd", type: "reload_config", args: { timeout_s: 2, poll_interval_s: 5 } } };
test("审计候选必须匹配租户节点动作操作者与合法 ID", () => {
  assert.equal(history.candidateId(row, "t", "node", "ops"), "cmd");
  for (const change of [{ tenant: "foreign" }, { object: "other" }, { actor: "other" }, { action: "other" }, { detail: {} }]) {
    assert.throws(() => history.candidateId({ ...row, ...change }, "t", "node", "ops"));
  }
});
test("参数匹配忽略对象键顺序但不忽略值、类型或数组顺序", () => {
  assert.equal(history.matchesAttempt(row, { type: "reload_config", args: { poll_interval_s: 5, timeout_s: 2 } }), true);
  assert.equal(history.matchesAttempt(row, { type: "reload_config", args: { poll_interval_s: 6, timeout_s: 2 } }), false);
  assert.equal(history.matchesAttempt(row, { type: "switch_model", args: row.detail.args }), false);
  const array = { ...row, detail: { type: "reload_skills", args: { skills: ["a", "b"] } } };
  assert.equal(history.matchesAttempt(array, { type: "reload_skills", args: { skills: ["b", "a"] } }), false);
});

const summary = { id: "cmd", node_id: "node", tenant_id: "t", type: "legacy", status: "pending" };
test("历史页拒绝异常形状、非法状态、重复ID、错筛选与不前进游标", () => {
  const page = { tenant_id: "t", node_id: "node", order: "newest_first", items: [summary], next_before_id: null };
  assert.equal(history.validateHistoryPage(page, "t", "node", "", "", null, []), null);
  for (const change of [
    { items: [null] }, { items: [{ ...summary, id: "" }] }, { items: [{ ...summary, type: "" }] },
    { items: [{ ...summary, status: "forged" }] }, { items: [summary, summary] },
    { items: [], next_before_id: 1 }, { items: Array(51).fill(summary) }, { tenant_id: "foreign" },
  ]) assert.throws(() => history.validateHistoryPage({ ...page, ...change }, "t", "node", "", "", null, []));
  assert.throws(() => history.validateHistoryPage(page, "t", "node", "ok", "", null, []));
  assert.throws(() => history.validateHistoryPage(page, "t", "node", "", "reload_config", null, []));
  assert.throws(() => history.validateHistoryPage(page, "t", "node", "", "", 10, [summary]));
  assert.throws(() => history.validateHistoryPage({ ...page, next_before_id: 10 }, "t", "node", "", "", 10, []));
});

test("回执范围、ID、状态和类型须匹配，保留未知旧指令类型", () => {
  assert.doesNotThrow(() => history.validateCommandSummary(summary, "t", "node", "cmd"));
  for (const value of [null, [], {}, { ...summary, status: "" }, { ...summary, id: "other" },
    { ...summary, node_id: "other" }, { ...summary, tenant_id: "foreign" }, { ...summary, type: 1 }]) {
    assert.throws(() => history.validateCommandSummary(value, "t", "node", "cmd"));
  }
});
