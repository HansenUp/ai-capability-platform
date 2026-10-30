import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";
const text = readFileSync(new URL("../src/run-outcome.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(text, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
const { outcome, runHint, eventStage, failureDetails, receiptGuidance, usageDetails, budgetDetails, usageCoverage, agentHint } = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`);
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

test("用量仅读取模型事件，缺失非法值未知，零仅为记录值", () => {
  assert.deepEqual(usageDetails([]), []);
  const rows = usageDetails([
    event("model.response", {}),
    {seq:2,type:"model.response",payload:{usage:{model:"mock",input_tokens:0,output_tokens:12,cost:0}}},
    {seq:3,type:"model.response",payload:{usage:{input_tokens:true,output_tokens:-1,cost:Infinity}}},
    event("tool.result", {usage:{input_tokens:999}}),
  ]);
  assert.equal(rows.length,3);
  assert.equal(rows[0].input,null); assert.equal(rows[0].model,"未知");
  assert.equal(rows[1].input,0); assert.equal(rows[1].output,12);
  assert.equal(rows[2].input,null); assert.equal(rows[2].output,null); assert.equal(rows[2].recordedCost,null);
});
test("预算只使用明确超限证据，不推断剩余金额或正常状态", () => {
  assert.deepEqual(budgetDetails([]),[]);
  assert.deepEqual(budgetDetails([event("run.error",{code:"OTHER",budget:"tokens"})]),[]);
  assert.deepEqual(budgetDetails([event("run.error",{code:"BUDGET_EXCEEDED",budget:"tokens",used:100,limit:100})]),[{seq:1,kind:"Token",used:100,limit:100}]);
});

test("辅助调用分类仅使用事件证据，不泄露摘要、不把压缩tokens当消费", () => {
  assert.deepEqual(usageCoverage([]), {summaryRecords:[],summaries:[],compactions:[]});
  const events = [
    {seq:1,type:"model.response",payload:{thinking_summary:"PRIVATE",usage:{input_tokens:10,output_tokens:2}}},
    {seq:2,type:"context.compacted",payload:{method:"llm",summary:"PRIVATE",tokens:{before:999,after:10}}},
    {seq:3,type:"context.compacted",payload:{method:"truncate"}},
    {seq:4,type:"context.compacted",payload:{method:"unknown"}},
  ];
  const coverage = usageCoverage(events);
  assert.deepEqual(coverage.summaries,[1]);
  assert.equal(coverage.compactions[0].method,"模型压缩");
  assert.match(coverage.compactions[1].method,/不能排除/);
  assert.equal(coverage.compactions[2].method,"方式未知");
  assert.equal(JSON.stringify(coverage).includes("PRIVATE"),false);
  assert.equal(usageDetails(events).length,1);
  assert.equal(usageDetails(events)[0].input,10);
});

test("压缩响应记录独立展示，不加入主响应统计", () => {
 const event = {seq:8,type:"context.compacted",payload:{method:"llm",model_usage_record:{source:"context_compaction",coverage:"response_record_only",billing_verified:false,response_model:"synthetic",usage:{input_reported:true,output_reported:true,cost_reported:true,input_tokens:20,output_tokens:4,cost:0.1}}}};
 assert.equal(usageCoverage([event]).compactions[0].record.input,20);
 assert.deepEqual(usageDetails([event]),[]);
 event.payload.model_usage_record.billing_verified=true;
 assert.equal(usageCoverage([event]).compactions[0].record,null);
});

test("思维摘要诊断独立于主模型，不输出正文", () => {
 const row={seq:9,type:"model.response",payload:{thinking_summary:"PRIVATE",hooks:[{name:"thinking_summary",point:"post_model",model_usage_record:{source:"thinking_summary",coverage:"response_record_only",billing_verified:false,response_model:"summary-test",usage:{input_reported:true,output_reported:true,cost_reported:true,input_tokens:40,output_tokens:6}}}]}};
 const coverage=usageCoverage([row]);
 assert.equal(coverage.summaryRecords[0].record.input,40);
 assert.equal(JSON.stringify(coverage).includes("PRIVATE"),false);
});

test("辅助用量旧默认零不可当作真实零，有明确来源的零可以展示", () => {
 const event={seq:1,type:"context.compacted",payload:{model_usage_record:{source:"context_compaction",coverage:"response_record_only",billing_verified:false,usage:{input_tokens:0,output_tokens:0,cost:0}}}};
 assert.equal(usageCoverage([event]).compactions[0].record.input,null);
 event.payload.model_usage_record.usage.input_reported=true;
 assert.equal(usageCoverage([event]).compactions[0].record.input,0);
});

test("发布导航仅提供Agent提示，拒绝数组、控制字符与超长输入", () => {
 assert.equal(agentHint("sales/a"),"sales/a");
 for(const value of [null,["a"],"a".repeat(65),"a\nb"]) assert.equal(agentHint(value),"");
});
