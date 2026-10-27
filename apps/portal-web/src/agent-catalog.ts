export interface PublishedAgent { agent_id: string; version: string; rollout: string }

/** Only Runtime decides visibility. Search narrows an already authorized result. */
export function searchAgents(agents: PublishedAgent[], query: string): PublishedAgent[] {
  const term = query.trim().toLocaleLowerCase();
  return agents.filter((agent) => agent.agent_id.toLocaleLowerCase().includes(term));
}

export async function readAgents(token: string, signal: AbortSignal, fetcher: typeof fetch = fetch): Promise<PublishedAgent[]> {
  const response = await fetcher("/v1/agents", {
    headers: { authorization: `Bearer ${token}` }, signal,
    cache: "no-store", redirect: "error", credentials: "omit",
  });
  if (!response.ok) throw new Error(`加载 Agent 列表失败：HTTP ${response.status}`);
  const rows: unknown = await response.json();
  if (!Array.isArray(rows) || rows.some((row) => !row || typeof row.agent_id !== "string"
    || !row.agent_id || typeof row.version !== "string" || typeof row.rollout !== "string")
    || new Set(rows.map((row) => row.agent_id)).size !== rows.length) {
    throw new Error("Agent 列表格式无效，请重新查询。");
  }
  if (signal.aborted) throw new DOMException("读取已取消", "AbortError");
  return rows;
}
