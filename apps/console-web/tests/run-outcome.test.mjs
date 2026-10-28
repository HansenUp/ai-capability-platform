import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";
const text = readFileSync(new URL("../src/run-outcome.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(text, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
const { outcome, runHint, eventStage, failureDetails, receiptGuidance } = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`);
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

test("失败解释绑定事件，不展开敏感正文、不被后续成功覆盖", () => {
  const rows = failureDetails([
    {seq:2,type:"run.error",payload:{code:"UNKNOWN_TOOL_RESULT",message:"PRIVATE"}},
    {seq:3,type:"tool.result",payload:{status:"denied",output:"PRIVATE"}},
    {seq:4,type:"tool.result",payload:{status:"ok"}},
    {seq:5,type:"run.error",payload:{code:"PRIVATE_UNKNOWN",message:"PRIVATE"}},
  ]);
  assert.deepEqual(rows.map(row => row.seq),[2,3,5]);
  assert.match(rows[0].reason,/必须对账/); assert.equal(JSON.stringify(rows).includes("PRIVATE"),false);
  assert.deepEqual(failureDetails([]),[]);
  assert.match(failureDetails([event("run.error",{code:"__proto__"})])[0].reason,/存在运行异常/);
});
test("回执指引不以终结状态或缺失回执自动解锁", () => {
  assert.match(receiptGuidance("PREPARED"),/不等于已执行/);
  for (const status of ["COMMITTED","REJECTED","OBSERVED"]) assert.match(receiptGuidance(status),/仍不自动解锁/);
  assert.match(receiptGuidance("UNKNOWN"),/保持未决写锁/);
  assert.match(receiptGuidance(),/不能证明/);
});
