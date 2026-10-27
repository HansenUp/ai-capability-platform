export interface CommandSummary {
  id: string; node_id: string; tenant_id: string; type: string; status: string;
  created_by: string; created_at: string; acked_at: string | null;
}
export interface AuditCandidate {
  id: number; ts: string; actor: string; tenant: string; action: string; object: string;
  detail: { id?: string; type?: string; args?: unknown };
}
export function historyPath(node: string, status: string, type: string, cursor: number | null): string {
  if (!node.trim()) throw new Error("请输入节点 ID。");
  const query = new URLSearchParams({ limit: "50" });
  if (status) query.set("status", status);
  if (type) query.set("type", type);
  if (cursor !== null) query.set("before_id", String(cursor));
  return `/nodes/${encodeURIComponent(node.trim())}/command-history?${query}`;
}
export function auditPath(node: string, actor: string, since: string, until: string, cursor: number | null): string {
  if (!node.trim()) throw new Error("请输入原目标节点 ID。");
  const query = new URLSearchParams({ limit: "50", action: "node.command", object: node.trim() });
  if (actor.trim()) query.set("actor", actor.trim());
  for (const [key, value] of [["since", since], ["until", until]]) {
    if (value.trim()) {
      if (!/(Z|[+-]\d{2}:\d{2})$/.test(value) || !Number.isFinite(Date.parse(value))) throw new Error("审计时间必须带明确时区（ISO 8601）。");
      query.set(key, new Date(value).toISOString());
    }
  }
  if (since && until && Date.parse(since) > Date.parse(until)) throw new Error("开始时间不能晚于结束时间。");
  if (cursor !== null) query.set("before_id", String(cursor));
  return `/audit/search?${query}`;
}
export function nextCursor(value: unknown, previous: number | null): number | null {
  if (value === null) return null;
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value <= 0 || (previous !== null && value >= previous)) {
    throw new Error("无效分页游标，停止读取。");
  }
  return value;
}
export function validateCommandSummary(value: unknown, tenant: string, node: string, id?: string): asserts value is CommandSummary {
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw new Error("回执范围或状态不匹配。");
  const row = value as Record<string, unknown>;
  if (row.tenant_id !== tenant || row.node_id !== node || typeof row.id !== "string" || !row.id || row.id.length > 128
    || (id !== undefined && row.id !== id) || typeof row.type !== "string" || !row.type || row.type.length > 32
    || !["pending", "ok", "error", "unsupported"].includes(row.status as string)) throw new Error("回执范围或状态不匹配。");
}
export function validateHistoryPage(value: unknown, tenant: string, node: string, status: string, type: string,
  previous: number | null, existing: CommandSummary[]): number | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw new Error("历史范围不匹配。");
  const page = value as Record<string, unknown>;
  if (page.tenant_id !== tenant || page.node_id !== node || page.order !== "newest_first" || !Array.isArray(page.items)) {
    throw new Error("历史范围不匹配。");
  }
  const ids = new Set(existing.map(row => row.id));
  for (const row of page.items) {
    try { validateCommandSummary(row, tenant, node); }
    catch { throw new Error("历史记录无效。"); }
    if (ids.has(row.id) || (status && row.status !== status) || (type && row.type !== type)) throw new Error("历史记录无效。");
    ids.add(row.id);
  }
  const next = nextCursor(page.next_before_id, previous);
  if (page.items.length > 50 || (!page.items.length && next !== null)) throw new Error("历史记录无效。");
  return next;
}
export function candidateId(row: AuditCandidate, tenant: string, node: string, actor: string): string {
  if (row.tenant !== tenant || row.object !== node || row.action !== "node.command" || (actor && row.actor !== actor)
    || typeof row.detail?.id !== "string" || !row.detail.id || row.detail.id.length > 128) throw new Error("审计候选范围或指令 ID 不匹配。");
  return row.detail.id;
}
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value !== null && typeof value === "object") {
    const object = value as Record<string, unknown>;
    return `{${Object.keys(object).sort().map(key => `${JSON.stringify(key)}:${canonical(object[key])}`).join(",")}}`;
  }
  return JSON.stringify(value) ?? "undefined";
}
export function matchesAttempt(row: AuditCandidate, body: { type: string; args: Record<string, unknown> }): boolean {
  return row.detail.type === body.type && canonical(row.detail.args) === canonical(body.args);
}
