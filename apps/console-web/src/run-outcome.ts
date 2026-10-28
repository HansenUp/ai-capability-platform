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
