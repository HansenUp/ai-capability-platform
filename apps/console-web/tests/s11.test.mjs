import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";

async function source(name) {
  const text = readFileSync(new URL(`../src/${name}.ts`, import.meta.url), "utf8");
  const result = ts.transpileModule(text, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
  return import(`data:text/javascript;base64,${Buffer.from(result.outputText).toString("base64")}`);
}
const config = await source("agent-config");
const debug = await source("run-debug");

test("结构化编辑保留扩展字段和红线其他字段", () => {
  const original = { tools: ["echo"], hooks: [{ name: "audit" }], redlines: { deny_tools: ["write"], extension: true }, context: { compaction: "truncate" } };
  const result = JSON.parse(config.updateConfig(JSON.stringify(original), "redlines", ["delete"]));
  assert.deepEqual(result, { ...original, redlines: { ...original.redlines, deny_tools: ["delete"] } });
  assert.deepEqual(JSON.parse(config.updateConfig(JSON.stringify(original), "tools", [])).tools, []);
});
test("无效 JSON 和不兼容红线失败关闭", () => {
  for (const text of ["null", "[]", "bad", "1"]) assert.throws(() => config.configObject(text));
  assert.throws(() => config.updateConfig('{"redlines":[]}', "redlines", []));
});
test("Skill 扩展元数据原样保存", () => {
  const skills = [{ name: "ticket-summary", version: "1", extension: { verified: false } }];
  assert.deepEqual(JSON.parse(config.updateConfig('{"other":1}', "skills", skills)), { other: 1, skills });
});
test("列表去重但保留顺序", () => assert.deepEqual(config.stringList("echo, write\necho\n"), ["echo", "write"]));
test("版本对比忽略对象键顺序而保留数组顺序", () => {
  assert.deepEqual(config.differences({ a: 1, b: 2 }, { b: 2, a: 1 }), []);
  assert.equal(config.differences({ tools: ["a", "b"] }, { tools: ["b", "a"] })[0].path, "/tools");
});
test("版本对比区分缺失和 null，路径转义", () => {
  assert.deepEqual(config.differences({}, { "a/b~": null }), [{ path: "/a~1b~0", before: "（缺失）", after: "null" }]);
});
const event = seq => ({ run_id: "run1", seq, type: "tool.decision", ts: "2026-10-21T00:00:00Z", payload: { decision: "deny" } });
test("事件增量排序去重并容许审批视图序号缺口", () => {
  assert.deepEqual(debug.mergeEvents([event(1)], [event(5), event(1)], "run1").map(value => value.seq), [1, 5]);
});
test("拒绝跨 Run 事件、非安全序号和冲突", () => {
  assert.throws(() => debug.mergeEvents([], [event(1)], "other"));
  assert.throws(() => debug.mergeEvents([], [event(Number.MAX_SAFE_INTEGER + 1)], "run1"));
  assert.throws(() => debug.mergeEvents([event(1)], [{ ...event(1), payload: {} }], "run1"));
});
test("Trace 链接仅允许既有本机目标，不接受事件 URL", () => {
  const id = "a".repeat(32);
  assert.equal(debug.traceLink("http://localhost:3000", "local/project", id), `http://localhost:3000/project/local%2Fproject/traces/${id}`);
  for (const base of ["javascript:alert(1)", "https://example.com", "http://user:pass@localhost:3000", "http://localhost:3000/redirect", "http://localhost:3000?x=1"]) assert.equal(debug.traceLink(base, "p", id), null);
  assert.equal(debug.traceLink("http://localhost:3000", "", id), null);
});
test("SSE done 仅取合法 Trace ID，不将 Run ID 当 Trace", () => {
  const id = "a".repeat(32);
  assert.equal(debug.doneTrace(`event: done\r\ndata: {"trace_id":"${id}"}`), id);
  assert.equal(debug.doneTrace('event: done\ndata: {"trace_id":null}'), null);
  assert.equal(debug.doneTrace('event: done\ndata: {"trace_id":"run1"}'), null);
  assert.equal(debug.doneTrace(`event: done\ndata: {"trace_id":"${"0".repeat(32)}"}`), null);
});
