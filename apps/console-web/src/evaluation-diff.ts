// Minimal persisted evaluation evidence only; no new evaluation or release decision.
export type CaseEvidence = {case_id: string; run_id: string; evidence: null | {judgement: string; reasons: string[]}};
export type CaseSide = {binding: {cases?: string[]}; children: CaseEvidence[]};
const labels: Record<string, string> = {
  RUN_NOT_COMPLETED: "Run未完成", EVENT_SEQUENCE_INCOMPLETE: "事件序列不完整",
  RUN_CREATION_EVIDENCE_INVALID: "Run创建证据无效", RUN_ERROR: "Run发生错误",
  EXECUTION_EVIDENCE_MISSING: "执行证据缺失", UNEXPECTED_INTERACTION: "出现非预期审批或输入交互",
  TOOL_ALLOWLIST_MISMATCH: "工具与允许范围不符", TOOL_RESULT_COUNT_MISMATCH: "工具结果数量不符",
  TOOL_STATUS_MISMATCH: "工具状态不符", TOOL_CALL_BINDING_MISMATCH: "工具调用绑定不符",
  EXPECTED_RESPONSE_MISSING: "缺少预期响应", SUCCESS_AFTER_TOOL_FAILURE: "工具失败后错误生成成功摘要",
  SALES_PROJECTION_MISMATCH: "销售字段投影不符", ORDER_METRICS_MISMATCH: "订单指标不符",
  CURRENCY_TOTAL_MISMATCH: "分币种合计不符", UNKNOWN_AMOUNT_REPLACED: "未知金额被替换",
  UNEXPECTED_ROW_DISCLOSURE: "出现非预期行明细", SYNTHETIC_SENSITIVE_FIELD_LEAK: "合成敏感字段泄露",
  INCOMPLETE_RESULT: "至少一侧结果未完整核验，不能计算差值",
};
const fields: Record<string, string> = {
  TENANT: "租户", SUBJECT: "操作者", AGENT_ID: "Agent", DATASET_ID: "冻结数据集",
  DATASET_VERSION: "数据集版本", DATASET_REVISION: "数据集修订", DATASET_DIGEST: "数据集内容摘要",
  CATALOG_REVISION: "用例目录版本", EVALUATOR_VERSION: "判定器版本", SCENARIO: "场景",
  CASES: "选中用例", PROFILE_DIGEST: "执行替身配置",
};
export function explainReason(code: string): string {
  if (labels[code]) return `${labels[code]}（${code}）`;
  for (const prefix of ["MISSING_", "DIFFERENT_"]) {
    const field = fields[code.slice(prefix.length)];
    if (code.startsWith(prefix) && field) return `${field}${prefix === "MISSING_" ? "证据缺失" : "不一致"}（${code}）`;
  }
  return "未识别原因，不能据此判定通过";
}
export function judgement(value?: string): string {
  return ({passed: "通过", not_passed: "未通过", not_verifiable: "不可核验"} as Record<string, string>)[value ?? ""] ?? "未知（证据缺失）";
}
function side(children: CaseEvidence[], id: string) {
  const matches = children.filter(child => child.case_id === id);
  if (matches.length !== 1) return {label: matches.length ? "证据冲突（重复用例）" : "未知（证据缺失）", reasons: [], value: null};
  const value = matches[0].evidence;
  return {label: judgement(value?.judgement), reasons: value?.reasons.map(explainReason) ?? [], value};
}
export function caseDifferences(left: CaseSide, right: CaseSide, comparable: boolean) {
  const ids = [...new Set([...(left.binding.cases ?? []), ...(right.binding.cases ?? []),
    ...left.children.map(child => child.case_id), ...right.children.map(child => child.case_id)])].sort();
  return ids.map(id => {
    const a = side(left.children, id), b = side(right.children, id);
    const known = (value: typeof a.value) => value && ["passed", "not_passed"].includes(value.judgement);
    let change = "未知（缺失、冲突或不可核验证据）";
    if (!comparable) change = "不可比（批次条件或结果证据不满足）";
    else if (known(a.value) && known(b.value)) {
      const reasons = (value: NonNullable<typeof a.value>) => JSON.stringify([...new Set(value.reasons)].sort());
      change = a.value!.judgement !== b.value!.judgement ? `${a.label} → ${b.label}`
        : reasons(a.value!) !== reasons(b.value!) ? "判定相同，原因变化" : "判定与原因相同";
    }
    return {id, left: a, right: b, change};
  });
}
