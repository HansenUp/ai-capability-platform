import test from "node:test";
import assert from "node:assert/strict";
import {caseDifferences, explainReason} from "../src/evaluation-diff.ts";
const child = (id, judgement="passed", reasons=[]) => ({case_id:id,run_id:"synthetic",evidence:{judgement,reasons}});
const side = children => ({binding:{cases:["success","failure"]},children});
test("aligns by case ID and preserves left/right transition rather than list order",()=>{
 const rows=caseDifferences(side([child("success"),child("failure")]),side([child("failure"),child("success","not_passed",["EXPECTED_RESPONSE_MISSING"])]),true);
 assert.equal(rows.find(r=>r.id==="success").change,"通过 → 未通过");
 assert.equal(rows.find(r=>r.id==="failure").change,"判定与原因相同");
 assert.match(rows.find(r=>r.id==="success").right.reasons[0],/缺少预期响应/);
});
test("missing, duplicate and unverifiable evidence never implies a pass or zero",()=>{
 for(const children of [[],[child("success"),child("success")],[{...child("success"),evidence:null}],[child("success","not_verifiable")]]) {
  const row=caseDifferences(side([child("success")]),side(children),true).find(r=>r.id==="success");
  assert.match(row.change,/未知/);
 }
 const missing=caseDifferences(side([]),side([]),true);assert.equal(missing.length,2);
 assert.ok(missing.every(r=>r.left.label.includes("未知")));
});
test("incomparable batches show evidence but do not assert transitions",()=>{
 const row=caseDifferences(side([child("success")]),side([child("success","not_passed")]),false).find(r=>r.id==="success");
 assert.match(row.change,/不可比/);assert.equal(row.right.label,"未通过");
});
test("reason order does not fabricate changes; changed reasons remain visible",()=>{
 const a=side([child("success","not_passed",["RUN_ERROR","EXPECTED_RESPONSE_MISSING"])]);
 const b=side([child("success","not_passed",["EXPECTED_RESPONSE_MISSING","RUN_ERROR"])]);
 assert.equal(caseDifferences(a,b,true).find(r=>r.id==="success").change,"判定与原因相同");
 b.children[0].evidence.reasons=["RUN_ERROR"];
 assert.equal(caseDifferences(a,b,true).find(r=>r.id==="success").change,"判定相同，原因变化");
});
test("explains mismatches and missing conditions without exposing unknown arbitrary text",()=>{
 assert.match(explainReason("DIFFERENT_DATASET_DIGEST"),/数据集内容摘要不一致/);
 assert.match(explainReason("MISSING_EVALUATOR_VERSION"),/判定器版本证据缺失/);
 assert.match(explainReason("INCOMPLETE_RESULT"),/未完整核验/);
 assert.equal(explainReason("secret body"),"未识别原因，不能据此判定通过");
});
