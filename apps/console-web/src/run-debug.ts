export interface RunEvent { run_id: string; seq: number; type: string; ts: string; payload: Record<string, unknown> }
const validTrace = (value: unknown): value is string => typeof value === "string" && /^[a-f0-9]{32}$/i.test(value) && !/^0+$/.test(value);
export function mergeEvents(previous: RunEvent[], incoming: RunEvent[], runId: string): RunEvent[] {
  const events = new Map(previous.map(event => [event.seq, event]));
  for (const event of incoming) {
    if (event.run_id !== runId || !Number.isSafeInteger(event.seq) || event.seq < 1 || typeof event.type !== "string" || !event.payload || typeof event.payload !== "object" || Array.isArray(event.payload))
      throw new Error("事件响应不符合 Run 契约");
    const existing = events.get(event.seq);
    if (existing && JSON.stringify(existing) !== JSON.stringify(event)) throw new Error("同序号事件发生冲突，请重新读取");
    events.set(event.seq, event);
  }
  return [...events.values()].sort((before, after) => before.seq - after.seq);
}

export function traceLink(base: string, project: string, traceId: string): string | null {
  try {
    const url = new URL(base);
    if (!["http:", "https:"].includes(url.protocol) || !["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)
      || url.username || url.password || url.search || url.hash || url.pathname !== "/" || !project || !validTrace(traceId)) return null;
    return `${url.origin}/project/${encodeURIComponent(project)}/traces/${traceId}`;
  } catch { return null; }
}

export function doneTrace(frame: string): string | null {
  const lines = frame.replace(/\r\n/g, "\n").split("\n");
  if (!lines.some(line => line === "event: done")) return null;
  const data = JSON.parse(lines.filter(line => line.startsWith("data:")).map(line => line.slice(5).trimStart()).join("\n"));
  return validTrace(data.trace_id) ? data.trace_id : null;
}
