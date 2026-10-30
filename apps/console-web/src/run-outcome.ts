interface Event { seq: number; type: string; payload: Record<string, unknown> }
export function outcome(events: Event[]) {
  const results = events.filter(e => e.type === "tool.result");
  return {
    ok: results.filter(e => e.payload.status === "ok").length,
    failed: results.filter(e => ["error", "denied"].includes(String(e.payload.status))).length,
    unknown: results.filter(e => !["ok", "error", "denied"].includes(String(e.payload.status))).length,
    texts: events.filter(e => e.type === "model.response" && typeof e.payload.text === "string" && e.payload.text.trim()).length,
  };
}
export function eventStage(type: string): string {
  return ({ "tool.requested": "请求", "tool.decision": "授权判定", "tool.executing": "开始执行", "tool.result": "工具结果", "model.response": "助手输出" } as Record<string, string>)[type] ?? "运行事件";
}
export function runHint(value: unknown): string {
  return typeof value === "string" && /^[A-Za-z0-9_-]{1,40}$/.test(value) ? value : "";
}

const reasons: Record<string, string> = {
  UNKNOWN_TOOL_RESULT: "工具执行结果未知，必须对账；不能推断未执行。",
  BUDGET_EXCEEDED: "运行预算已超出限制。",
  DATA_CLASS_MODEL_BLOCKED: "当前模型不允许接收此数据分类。",
  POOL_EXHAUSTED: "执行资源池没有可用容量。",
  HARNESS_VERSION_MISMATCH: "执行器版本与任务快照不一致。",
  RUN_SNAPSHOT_MISSING: "运行所需快照缺失。",
};
export function failureDetails(events: Event[]) {
  return events.flatMap(event => {
    if (event.type === "run.error") {
      const code = typeof event.payload.code === "string" ? event.payload.code : "";
      return [{ seq: event.seq, kind: "运行异常", reason: Object.prototype.hasOwnProperty.call(reasons, code) ? reasons[code] : "存在运行异常；具体原因请核对当前身份可见的原始事件。" }];
    }
    if (event.type === "tool.result" && ["error", "denied"].includes(String(event.payload.status))) {
      return [{ seq: event.seq, kind: event.payload.status === "denied" ? "工具拒绝" : "工具失败",
        reason: event.payload.status === "denied" ? "工具请求被拒绝；请核对授权判定与原始结果。" : "工具返回错误；仅凭此事件不能判断是否存在业务副作用。" }];
    }
    return [];
  });
}
export function receiptGuidance(status?: string): string {
  if (status === "PREPARED") return "已登记不等于已执行。先只读核对活动回执，不自动提交或重新登记。";
  if (status === "COMMITTED" || status === "REJECTED" || status === "OBSERVED") return "已取得终结状态仍不自动解锁；核对绑定证据后由原操作者预览并确认收口。";
  if (status) return "回执尚需核对；保持未决写锁，仅使用现有只读恢复入口，不重新发送业务写。";
  return "当前结果区不能证明主体没有未决写锁；请在工作台只读核对写账本。";
}

// These are persisted runtime records, not provider billing receipts.
export function usageDetails(events: Event[]) {
  const number = (value: unknown): number | null => typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;
  return events.filter(e => e.type === "model.response").map(e => {
    const raw = e.payload.usage;
    const usage = raw && typeof raw === "object" && !Array.isArray(raw) ? raw as Record<string, unknown> : {};
    return { seq: e.seq, model: typeof usage.model === "string" && usage.model.trim() ? usage.model : "未知",
      input: number(usage.input_tokens), output: number(usage.output_tokens),
      recordedCost: typeof usage.cost === "number" && Number.isFinite(usage.cost) && usage.cost >= 0 ? usage.cost : null };
  });
}
export function budgetDetails(events: Event[]) {
  return events.filter(e => e.type === "run.error" && e.payload.code === "BUDGET_EXCEEDED").map(e => ({
    seq: e.seq, kind: e.payload.budget === "tokens" ? "Token" : e.payload.budget === "wall" ? "运行时长（秒）" : "未知类型",
    used: typeof e.payload.used === "number" && Number.isFinite(e.payload.used) && e.payload.used >= 0 ? e.payload.used : null,
    limit: typeof e.payload.limit === "number" && Number.isFinite(e.payload.limit) && e.payload.limit >= 0 ? e.payload.limit : null,
  }));
}

export function usageCoverage(events: Event[]) {
  return {
    summaryRecords: events.filter(e => e.type === "model.response").flatMap(e => {
      const hooks = Array.isArray(e.payload.hooks) ? e.payload.hooks : [];
      return hooks.flatMap((hook, index) => {
        if (!hook || typeof hook !== "object" || hook.name !== "thinking_summary" || hook.point !== "post_model") return [];
        const raw = hook.model_usage_record;
        if (!raw || raw.source !== "thinking_summary") return [];
        const record = compactionUsage({...e,payload:{model_usage_record:{...raw,source:"context_compaction"}}});
        return record ? [{seq:e.seq,index,record}] : [];
      });
    }),
    summaries: events.filter(e => e.type === "model.response" && typeof e.payload.thinking_summary === "string" && e.payload.thinking_summary.length > 0).map(e => e.seq),
    compactions: events.filter(e => e.type === "context.compacted").map(e => ({seq: e.seq,
      record: compactionUsage(e),
      method: e.payload.method === "llm" ? "模型压缩" : e.payload.method === "truncate" ? "截断结果（不能排除此前模型尝试）" : "方式未知"})),
  };
}

function compactionUsage(event: Event) {
  const raw = event.payload.model_usage_record;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const record = raw as Record<string, unknown>;
  if (record.source !== "context_compaction" || record.coverage !== "response_record_only" || record.billing_verified !== false) return null;
  const usage = record.usage;
  if (!usage || typeof usage !== "object" || Array.isArray(usage)) return null;
  const values = usage as Record<string, unknown>;
  return usageDetails([{seq:event.seq,type:"model.response",payload:{usage:{
    model:record.response_model,
    input_tokens:values.input_reported === true ? values.input_tokens : null,
    output_tokens:values.output_reported === true ? values.output_tokens : null,
    cost:values.cost_reported === true ? values.cost : null,
  }}}])[0];
}

export function agentHint(value: unknown): string {
  return typeof value === "string" && value.length > 0 && value.length <= 64 && !/[\x00-\x1f\x7f]/.test(value) ? value : "";
}
