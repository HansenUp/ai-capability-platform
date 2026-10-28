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
