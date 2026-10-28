import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";
const text = readFileSync(new URL("../src/run-outcome.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(text, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
const { outcome, runHint, eventStage } = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`);
const event = (type, payload) => ({ seq: 1, type, payload });
test("空事件不提供成功证据；助手文本不替代工具结果", () => {
  assert.deepEqual(outcome([]), { ok: 0, failed: 0, unknown: 0, texts: 0 });
  assert.deepEqual(outcome([event("model.response", { text: "已完成" })]), { ok: 0, failed: 0, unknown: 0, texts: 1 });
});
test("部分失败与未知状态分别保留，不按最后一次成功抹掉", () => {
  assert.deepEqual(outcome([event("tool.result", { status: "error" }), event("tool.result", { status: "ok" }), event("tool.result", {}), event("tool.result", { status: "denied" })]), { ok: 1, failed: 2, unknown: 1, texts: 0 });
});
test("导航仅接受Run标识，拒绝URL、数组和过长输入", () => {
  assert.equal(runHint("run_test-1"), "run_test-1");
  for (const value of ["https://other", ["run_a"], null, "a".repeat(41), "../x"]) assert.equal(runHint(value), "");
  assert.equal(eventStage("tool.decision"), "授权判定");
  assert.equal(eventStage("unknown"), "运行事件");
});
