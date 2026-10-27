export interface ConsoleOperator { user: string; role: string; tenant: string }

export function traceQuery(agent: string, since: string, until: string): string {
  const start = new Date(since), end = new Date(until);
  const duration = end.getTime() - start.getTime();
  if (!agent.trim() || agent.trim().length > 64 || !Number.isFinite(duration) || duration <= 0 || duration > 7 * 86400000) {
    throw new Error("请选择 Agent 和递增的时间窗（最长七天）。");
  }
  return `/metrics/traces?${new URLSearchParams({ agent_id: agent.trim(), since: start.toISOString(), until: end.toISOString() })}`;
}

export function commandPath(node: string, command: string): string {
  if (!node.trim() || !command.trim()) throw new Error("请输入节点和指令 ID。");
  return `/nodes/${encodeURIComponent(node.trim())}/commands/${encodeURIComponent(command.trim())}`;
}

export function metric(value: unknown): string {
  if (typeof value === "number" && Number.isFinite(value) && value >= 0) return String(value);
  if (typeof value === "string" && /^\d+(\.\d+)?$/.test(value)) return value;
  return "未知";
}

export function readSession(development: boolean, fetcher: typeof fetch = fetch) {
  let generation = 0;
  let controller: AbortController | null = null;
  function invalidate() { generation += 1; controller?.abort(); controller = null; }
  return {
    invalidate,
    async read<T>(path: string, operator: ConsoleOperator): Promise<T> {
      invalidate();
      if (!development) throw new Error("仅支持本机开发身份；生产统一登录尚未接入。");
      if (!path.startsWith("/") || path.startsWith("//")) throw new Error("无效 Console 路径。");
      const current = generation;
      controller = new AbortController();
      const response = await fetcher(`/console${path}`, { method: "GET", cache: "no-store", signal: controller.signal,
        headers: { "X-Console-User": operator.user, "X-Console-Role": operator.role, "X-Console-Tenant": operator.tenant } });
      if (!response.ok) throw new Error(`HTTP ${response.status}：数据不可用；未保留旧结果。`);
      const data = await response.json();
      if (current !== generation) throw new DOMException("请求上下文已改变", "AbortError");
      return data as T;
    },
  };
}
