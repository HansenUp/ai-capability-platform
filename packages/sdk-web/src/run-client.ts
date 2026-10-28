export interface WorkbenchRun { run_id: string; state: string; subject: string; agent_id: string; last_seq: number }
export interface WorkbenchEvent { run_id: string; seq: number; type: string; payload: Record<string, unknown> }
export interface ApprovalCard { call_id: string; params_hash: string; tool: string; input: unknown }
export interface InputCard { kind: string; call_id?: string; prompt: string; schema?: Record<string, unknown> | null }
export interface WorkbenchIdentity { scope_id: string; tenant_id: string; subject: string; auth_method: "user"; protocol: "workbench-v1" }
export interface WriteAttempt {
  scope_id: string; attempt_id: string; action: "credential" | "approve" | "reject" | "answer" | "create" | "feedback"; run_id: string; request_seq: number; fingerprint: string;
  score?: number; score_revision?: number; payload_hash?: string;
  call_id?: string; params_hash?: string; value_hmac?: string;
  create_agent?: string; create_version?: string; create_model?: string; create_snapshot?: string; create_reason?: string;
  status: "PREPARED" | "COMMITTED" | "REJECTED_NO_EFFECT" | "PENDING_OBSERVATION" | "OBSERVED"; revision: number; evidence_seq: number | null;
  rejection: string | null; acknowledged: boolean;
}
function attempt(value: unknown, scope: string, mutation = false): WriteAttempt {
  const row = value as WriteAttempt;
  if (row?.action === "feedback") {
    if (row.scope_id !== scope || !/^[a-f0-9]{64}$/.test(scope)
      || !/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(row.attempt_id)
      || typeof row.run_id !== "string" || !row.run_id || row.run_id.length > 40
      || !Number.isSafeInteger(row.request_seq) || row.request_seq < 1 || !/^[a-f0-9]{64}$/.test(row.fingerprint)
      || row.payload_hash !== row.fingerprint || !Number.isSafeInteger(row.score_revision) || row.score_revision! < 1
      || typeof row.score !== "number" || ![-1, 0, 1].includes(row.score) || typeof row.acknowledged !== "boolean"
      || row.evidence_seq !== null || row.rejection !== null || row.call_id !== undefined || row.params_hash !== undefined
      || row.value_hmac !== undefined || row.create_agent !== undefined || row.create_version !== undefined || row.create_model !== undefined
      || row.create_snapshot !== undefined || row.create_reason !== undefined
      || (row.status === "PREPARED" ? row.revision !== 1 || row.acknowledged
        : row.status === "PENDING_OBSERVATION" ? row.revision !== 2 || row.acknowledged
          : row.status === "OBSERVED" ? row.revision !== (row.acknowledged ? 4 : 3) : true)) {
      throw new RuntimeRequestError("反馈回执绑定无效；保持写锁，不重发。", 200, mutation);
    }
    return row;
  }
  if (!row || row.scope_id !== scope || !/^[a-f0-9]{64}$/.test(scope) || !["credential", "approve", "reject", "answer", "create"].includes(row.action)
    || (row.action === "create" ? row.call_id !== undefined || row.params_hash !== undefined || !/^[a-f0-9]{64}$/.test(row.value_hmac ?? "")
      || typeof row.create_agent !== "string" || !row.create_agent || row.create_agent.length > 64
      || typeof row.create_version !== "string" || !row.create_version || row.create_version.length > 32
      || typeof row.create_model !== "string" || !row.create_model || row.create_model.length > 64
      || !/^[a-f0-9]{64}$/.test(row.create_snapshot ?? "") || typeof row.create_reason !== "string" || !row.create_reason || row.create_reason.length > 32
      : row.create_agent !== undefined || row.create_version !== undefined || row.create_model !== undefined || row.create_snapshot !== undefined || row.create_reason !== undefined)
    || (row.action === "create" ? false : row.action === "credential" ? row.call_id !== undefined || row.params_hash !== undefined || row.value_hmac !== undefined
      : typeof row.call_id !== "string" || !row.call_id || row.call_id.length > 128
        || (row.action === "answer" ? row.params_hash !== undefined || !/^[a-f0-9]{64}$/.test(row.value_hmac ?? "")
          : row.value_hmac !== undefined || typeof row.params_hash !== "string" || !row.params_hash || row.params_hash.length > 64))
    || typeof row.attempt_id !== "string" || !/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(row.attempt_id)
    || typeof row.run_id !== "string" || !row.run_id || !Number.isSafeInteger(row.request_seq) || (row.action === "create" ? row.request_seq !== 0 : row.request_seq < 1)
    || !/^[a-f0-9]{64}$/.test(row.fingerprint) || !Number.isSafeInteger(row.revision) || row.revision < 1
    || typeof row.acknowledged !== "boolean" || !["PREPARED", "COMMITTED", "REJECTED_NO_EFFECT"].includes(row.status)
    || (row.status === "PREPARED" && (row.acknowledged || row.evidence_seq !== null || row.rejection !== null))
    || (row.status === "COMMITTED" && (!Number.isSafeInteger(row.evidence_seq) || row.evidence_seq! <= row.request_seq || row.rejection !== null))
    || (row.action === "create" && row.status === "COMMITTED" && row.evidence_seq !== 1)
    || (row.status === "REJECTED_NO_EFFECT" && (row.evidence_seq !== null || !["VALIDATION_REJECTED", "STALE_WRITER"].includes(row.rejection!)))) {
    throw new RuntimeRequestError("工作台回执范围或格式无效；保持写锁，不重发。", 200, mutation);
  }
  return row;
}
export class RuntimeRequestError extends Error {
  constructor(message: string, public readonly status: number, public readonly outcomeUnknown = false) { super(message); }
}
function assertReading(signal?: AbortSignal) {
  if (signal?.aborted) throw new DOMException("Runtime 读取已取消", "AbortError");
}
export class RuntimeRunClient {
  constructor(private readonly headers: () => Promise<Record<string, string>>, private readonly fetcher: typeof fetch = fetch) {}
  private async request(path: string, method = "GET", body?: unknown, expectedStatus = 200, signal?: AbortSignal): Promise<unknown> {
    assertReading(signal);
    const headers = await this.headers();
    assertReading(signal);
    const fetcher = this.fetcher;
    let response: Response;
    try {
      response = await fetcher(path, { method, headers: { ...headers, "Content-Type": "application/json" }, cache: "no-store",
        redirect: "error", ...(signal ? { signal } : {}), body: body === undefined ? undefined : JSON.stringify(body) });
    } catch { assertReading(signal); throw new RuntimeRequestError(method === "GET" ? "Runtime 读取失败" : "写请求结果未知；禁止自动重试，请按 Run ID 刷新核对。", 0, method !== "GET"); }
    assertReading(signal);
    if (!response.ok) throw new RuntimeRequestError(`Runtime HTTP ${response.status}；未取得授权或状态已变化，请刷新核对。`, response.status,
      method !== "GET" && response.status >= 500);
    if (response.status !== expectedStatus) throw new RuntimeRequestError("Runtime 响应状态与约定不符，请按 Run ID 核对。", response.status, method !== "GET");
    try { const result = await response.json(); assertReading(signal); return result; }
    catch { assertReading(signal); throw new RuntimeRequestError("Runtime 响应无效；请刷新核对，不自动重试写请求。", response.status, method !== "GET"); }
  }
  async ledger(signal?: AbortSignal): Promise<{ identity: WorkbenchIdentity; active: WriteAttempt | null }> {
    const identity = await this.request("/v1/workbench/identity", "GET", undefined, 200, signal) as WorkbenchIdentity;
    if (!identity || identity.protocol !== "workbench-v1" || identity.auth_method !== "user"
      || !/^[a-f0-9]{64}$/.test(identity.scope_id) || typeof identity.subject !== "string" || !identity.subject
      || typeof identity.tenant_id !== "string" || !identity.tenant_id) throw new Error("工作台身份格式无效，禁止新写。");
    const result = await this.request("/v1/workbench/attempts", "GET", undefined, 200, signal) as { scope_id: string; active: unknown };
    if (!result || result.scope_id !== identity.scope_id) throw new Error("工作台账本与身份不匹配，禁止新写。");
    let active: WriteAttempt | null;
    const raw = result.active as WriteAttempt | null;
    if (raw?.action === "feedback") {
      if (raw.scope_id !== identity.scope_id || !/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(raw.attempt_id)) throw new Error("反馈活动锁范围无效。");
      active = await this.feedbackReceipt(raw.attempt_id, identity.scope_id, signal);
      if (active.run_id !== raw.run_id || active.fingerprint !== raw.fingerprint || active.revision < raw.revision) throw new Error("反馈活动锁与明细冲突。");
    } else active = raw === null ? null : attempt(raw, identity.scope_id);
    if (active?.acknowledged) throw new Error("活动账本与回执矛盾，保持写锁。");
    return { identity, active };
  }
  async prepareCredential(runId: string, scope: string, attemptId: string = crypto.randomUUID()): Promise<WriteAttempt> {
    if (!runId.trim()) throw new Error("Run ID 必填。");
    const result = attempt(await this.request("/v1/workbench/attempts", "POST", { run_id: runId.trim(), attempt_id: attemptId }), scope, true);
    if (result.attempt_id !== attemptId || result.run_id !== runId.trim() || result.action !== "credential" || result.status !== "PREPARED" || result.acknowledged) {
      throw new RuntimeRequestError("写前登记绑定不匹配；只读核对账本，不重发。", 200, true);
    }
    return result;
  }
  async prepareApproval(runId: string, card: ApprovalCard, approved: boolean, scope: string, attemptId: string = crypto.randomUUID()): Promise<WriteAttempt> {
    if (!runId.trim() || !card.call_id || !card.params_hash) throw new Error("Run 与审批参数指纹必填。");
    const action = approved ? "approve" : "reject";
    const result = attempt(await this.request("/v1/workbench/attempts", "POST", { run_id: runId.trim(), attempt_id: attemptId,
      action, call_id: card.call_id, params_hash: card.params_hash }), scope, true);
    if (result.attempt_id !== attemptId || result.run_id !== runId.trim() || result.action !== action || result.call_id !== card.call_id
      || result.params_hash !== card.params_hash || result.status !== "PREPARED" || result.acknowledged) {
      throw new RuntimeRequestError("审批登记绑定不匹配；只读核对，不重发。", 200, true);
    }
    return result;
  }
  async prepareAnswer(runId: string, card: InputCard, value: unknown, scope: string, attemptId: string = crypto.randomUUID()): Promise<WriteAttempt> {
    if (!runId.trim() || card.kind !== "answer" || !card.call_id || value === undefined) throw new Error("Run、回答卡片与原始 JSON 值必填。");
    const result = attempt(await this.request("/v1/workbench/attempts", "POST", { run_id: runId.trim(), attempt_id: attemptId,
      action: "answer", call_id: card.call_id, value }), scope, true);
    if (result.attempt_id !== attemptId || result.run_id !== runId.trim() || result.action !== "answer" || result.call_id !== card.call_id
      || result.status !== "PREPARED" || result.acknowledged) {
      throw new RuntimeRequestError("回答登记绑定不匹配；只读核对，不重发。", 200, true);
    }
    return result;
  }
  async prepareCreate(agent: string, text: string, scope: string, attemptId: string = crypto.randomUUID()): Promise<WriteAttempt> {
    if (!agent.trim() || !text.trim()) throw new Error("Agent 和消息必填。");
    const result = attempt(await this.request("/v1/workbench/create-attempts", "POST", { agent_id: agent.trim(), text, attempt_id: attemptId }), scope, true);
    if (result.attempt_id !== attemptId || result.action !== "create" || result.create_agent !== agent.trim() || result.status !== "PREPARED" || result.acknowledged) {
      throw new RuntimeRequestError("创建登记绑定不匹配；只读核对，不重发。", 200, true);
    }
    return result;
  }
  async settleAttempt(row: WriteAttempt, acknowledge = false, value?: unknown): Promise<WriteAttempt> {
    if (row.action === "feedback") return this.feedbackAction(row, acknowledge ? "acknowledge" : "execute");
    attempt(row, row.scope_id);
    if (row.acknowledged || (acknowledge ? row.status === "PREPARED" : row.status !== "PREPARED")) throw new Error("回执状态不允许该动作。");
    if (row.action === "answer" && !acknowledge && value === undefined) throw new Error("请重新提供登记时的原始 JSON 回答；不能自动恢复或重发。");
    if (row.action === "create" && !acknowledge && (typeof value !== "string" || !value.trim())) throw new Error("请重新提供登记时的原始消息；不能自动恢复或重发。");
    const result = attempt(await this.request(`/v1/workbench/attempts/${row.attempt_id}/${acknowledge ? "acknowledge" : "execute"}`,
      "POST", { fingerprint: row.fingerprint, revision: row.revision, ...(["answer", "create"].includes(row.action) && !acknowledge ? { value } : {}) }), row.scope_id, true);
    if (result.attempt_id !== row.attempt_id || result.run_id !== row.run_id || result.request_seq !== row.request_seq
      || result.action !== row.action || result.call_id !== row.call_id || result.params_hash !== row.params_hash || result.value_hmac !== row.value_hmac
      || result.create_agent !== row.create_agent || result.create_version !== row.create_version || result.create_model !== row.create_model
      || result.create_snapshot !== row.create_snapshot || result.create_reason !== row.create_reason
      || result.fingerprint !== row.fingerprint || result.revision <= row.revision || result.status === "PREPARED"
      || result.acknowledged !== acknowledge || (acknowledge && (result.status !== row.status || result.evidence_seq !== row.evidence_seq || result.rejection !== row.rejection))) {
      throw new RuntimeRequestError("终结回执绑定不匹配；保持写锁，只读核对。", 200, true);
    }
    return result;
  }
  async feedbackHead(runId: string, scope: string, signal?: AbortSignal): Promise<number> {
    const result = await this.request(`/v1/workbench/feedback-attempts/runs/${encodeURIComponent(runId)}/head`, "GET", undefined, 200, signal) as { scope_id: string; run_id: string; score_revision: number };
    if (!result || result.scope_id !== scope || result.run_id !== runId || !Number.isSafeInteger(result.score_revision) || result.score_revision < 0) throw new Error("反馈修订范围无效。");
    return result.score_revision;
  }
  async feedbackReceipt(id: string, scope: string, signal?: AbortSignal): Promise<WriteAttempt> {
    if (!/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(id)) throw new Error("反馈 attempt 无效。");
    const result = attempt(await this.request(`/v1/workbench/feedback-attempts/${id}`, "GET", undefined, 200, signal), scope);
    if (result.action !== "feedback" || result.attempt_id !== id) throw new Error("反馈回执不匹配。");
    return result;
  }
  async prepareFeedback(runId: string, score: number, revision: number, scope: string, id: string = crypto.randomUUID()): Promise<WriteAttempt> {
    if (!runId.trim() || !Number.isInteger(score) || ![-1, 0, 1].includes(score) || !Number.isSafeInteger(revision) || revision < 0) throw new Error("反馈参数无效。");
    const result = attempt(await this.request("/v1/workbench/feedback-attempts", "POST", { attempt_id: id, run_id: runId, score, expected_revision: revision }), scope, true);
    if (result.action !== "feedback" || result.attempt_id !== id || result.run_id !== runId || result.score !== score || result.score_revision !== revision + 1 || result.status !== "PREPARED") throw new RuntimeRequestError("反馈登记绑定不匹配，不重发。", 200, true);
    return result;
  }
  async feedbackAction(row: WriteAttempt, action: "execute" | "reconcile" | "acknowledge"): Promise<WriteAttempt> {
    attempt(row, row.scope_id);
    if (row.action !== "feedback" || row.acknowledged || row.status !== ({ execute: "PREPARED", reconcile: "PENDING_OBSERVATION", acknowledge: "OBSERVED" } as const)[action]) throw new Error("反馈状态不允许此动作。");
    const result = attempt(await this.request(`/v1/workbench/feedback-attempts/${row.attempt_id}/${action}`, "POST", { fingerprint: row.fingerprint, revision: row.revision }), row.scope_id, true);
    if (result.action !== "feedback" || result.attempt_id !== row.attempt_id || result.run_id !== row.run_id || result.request_seq !== row.request_seq
      || result.fingerprint !== row.fingerprint || result.score !== row.score || result.score_revision !== row.score_revision || result.payload_hash !== row.payload_hash
      || (action === "execute" ? result.status !== "PENDING_OBSERVATION" : action === "acknowledge" ? result.status !== "OBSERVED" || !result.acknowledged : !["PENDING_OBSERVATION", "OBSERVED"].includes(result.status))
      || (action !== "acknowledge" && result.acknowledged) || result.revision !== row.revision + (action === "reconcile" && result.status === "PENDING_OBSERVATION" ? 0 : 1)) throw new RuntimeRequestError("反馈状态或证据不匹配，保持写锁。", 200, true);
    return result;
  }
  async create(agent: string, text: string): Promise<string> {
    if (!agent.trim() || !text.trim()) throw new Error("Agent 和消息必填。");
    const result = await this.request("/v1/runs", "POST", { agent_id: agent.trim(), input: [{ role: "user", content: text }] }, 202) as { run_id?: unknown };
    if (!result || typeof result.run_id !== "string" || !result.run_id) throw new RuntimeRequestError("已受理但 Run ID 不可读；请从 Run 列表定位，不可盲目重发。", 202, true);
    return result.run_id;
  }
  async listMyRuns(subject: string, agent = "", state = "", cursor: string | null = null, signal?: AbortSignal): Promise<{ items: WorkbenchRun[]; next_cursor: string | null }> {
    const states = ["submitted", "working", "auth-required", "input-required", "completed", "failed", "canceled"];
    if (!subject || (state && !states.includes(state)) || (cursor !== null && (!cursor || cursor.length > 512))) throw new Error("任务查询范围无效。");
    const query = new URLSearchParams({ subject, limit: "20" });
    if (agent) query.set("agent_id", agent);
    if (state) query.set("state", state);
    if (cursor) query.set("cursor", cursor);
    const result = await this.request(`/v1/runs?${query}`, "GET", undefined, 200, signal) as { items: WorkbenchRun[]; next_cursor: string | null };
    if (!result || !Array.isArray(result.items) || result.items.length > 20
      || (result.next_cursor !== null && (typeof result.next_cursor !== "string" || !result.next_cursor || result.next_cursor.length > 512 || result.next_cursor === cursor))
      || result.items.some(row => !row || typeof row.run_id !== "string" || !row.run_id || typeof row.agent_id !== "string" || !row.agent_id
        || row.subject !== subject || (agent && row.agent_id !== agent) || (state && row.state !== state) || !states.includes(row.state)
        || !Number.isSafeInteger(row.last_seq) || row.last_seq < 0)
      || new Set(result.items.map(row => row.run_id)).size !== result.items.length) throw new Error("本人任务范围或分页响应不匹配。");
    return result;
  }
  async listOwnRuns(agent: string, subject: string, cursor: string | null = null, signal?: AbortSignal): Promise<{ items: WorkbenchRun[]; next_cursor: string | null }> {
    if (!agent || !subject || (cursor !== null && (!cursor || cursor.length > 512))) throw new Error("Run 列表范围或游标无效。");
    const query = new URLSearchParams({ agent_id: agent, subject, limit: "20" });
    if (cursor !== null) query.set("cursor", cursor);
    const result = await this.request(`/v1/runs?${query}`, "GET", undefined, 200, signal) as { items: WorkbenchRun[]; next_cursor: string | null };
    if (!result || !Array.isArray(result.items) || result.items.length > 20
      || (result.next_cursor !== null && (typeof result.next_cursor !== "string" || !result.next_cursor || result.next_cursor.length > 512 || result.next_cursor === cursor))
      || result.items.some(row => !row || typeof row.run_id !== "string" || !row.run_id || row.agent_id !== agent || row.subject !== subject
        || typeof row.state !== "string" || !Number.isSafeInteger(row.last_seq) || row.last_seq < 0)
      || new Set(result.items.map(row => row.run_id)).size !== result.items.length) throw new Error("Run 列表范围或分页格式不匹配。");
    return { items: result.items, next_cursor: result.next_cursor };
  }
  async inspect(runId: string, signal?: AbortSignal): Promise<{ run: WorkbenchRun; events: WorkbenchEvent[] }> {
    if (!runId.trim()) throw new Error("Run ID 必填。");
    const path = `/v1/runs/${encodeURIComponent(runId.trim())}`;
    const run = await this.request(path, "GET", undefined, 200, signal) as WorkbenchRun;
    if (!run || run.run_id !== runId.trim()) throw new Error("Run 读取范围或事件序号不匹配。");
    const events = await this.request(`${path}/events`, "GET", undefined, 200, signal) as WorkbenchEvent[];
    if (!Array.isArray(events) || events.some((event, index) => !event || event.run_id !== run.run_id || !Number.isSafeInteger(event.seq)
      || event.seq < 1 || (index > 0 && event.seq <= events[index - 1]?.seq) || typeof event.type !== "string"
      || !event.payload || typeof event.payload !== "object" || Array.isArray(event.payload))) {
      throw new Error("Run 读取范围或事件序号不匹配。");
    }
    return { run, events };
  }
  async *follow(runId: string, sinceSeq = 0, signal?: AbortSignal): AsyncGenerator<WorkbenchEvent> {
    const id = runId.trim();
    if (!id || !Number.isSafeInteger(sinceSeq) || sinceSeq < 0) throw new Error("Run ID 或续读序号无效。");
    assertReading(signal);
    const headers = await this.headers();
    assertReading(signal);
    const fetcher = this.fetcher;
    let response: Response;
    try {
      response = await fetcher(`/v1/runs/${encodeURIComponent(id)}/stream?since_seq=${sinceSeq}`, {
        method: "GET", headers: { ...headers, Accept: "text/event-stream" }, cache: "no-store", redirect: "error", signal,
      });
    } catch { assertReading(signal); throw new RuntimeRequestError("事件跟随连接失败；请手动续读，不自动重试。", 0); }
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    const cancel = () => { void reader?.cancel().catch(() => {}); };
    try {
      assertReading(signal);
      if (response.status !== 200) throw new RuntimeRequestError(`Runtime HTTP ${response.status}；事件跟随已停止。`, response.status);
      if (response.headers.get("content-type")?.split(";")[0].trim().toLowerCase() !== "text/event-stream" || !response.body) {
        throw new Error("事件跟随响应不是 SSE。");
      }
      reader = response.body.getReader();
      signal?.addEventListener("abort", cancel, { once: true });
      const decoder = new TextDecoder("utf-8", { fatal: true });
      let buffer = "", data: string[] = [], eventType = "", eventId = "", size = 0, cursor = sinceSeq;
      while (true) {
        assertReading(signal);
        const chunk = await reader.read();
        assertReading(signal);
        if (chunk.done) throw new Error("事件跟随断连，未收到 done；请从已确认序号手动续读。");
        buffer += decoder.decode(chunk.value, { stream: true });
        let boundary: RegExpExecArray | null;
        while ((boundary = /\r\n|\r|\n/.exec(buffer))) {
          if (boundary[0] === "\r" && boundary.index === buffer.length - 1) break;
          const line = buffer.slice(0, boundary.index);
          buffer = buffer.slice(boundary.index + boundary[0].length);
          if (line === "") {
            if (data.length) {
              const value = JSON.parse(data.join("\n"));
              if (eventType === "done") {
                if (!value || value.run_id !== id || value.type !== "done" || !["end_turn", "max_tokens", "cancelled", "error", "awaiting"].includes(value.stop_reason)) {
                  throw new Error("SSE done 范围或格式不匹配。");
                }
                return;
              }
              if (!value || value.run_id !== id || !Number.isSafeInteger(value.seq) || value.seq <= cursor
                || eventId !== String(value.seq) || typeof value.type !== "string" || value.type !== eventType
                || !value.payload || typeof value.payload !== "object" || Array.isArray(value.payload)) {
                throw new Error("SSE 事件范围、序号或格式不匹配。");
              }
              cursor = value.seq;
              assertReading(signal);
              yield value as WorkbenchEvent;
              assertReading(signal);
            }
            data = []; eventType = ""; eventId = ""; size = 0;
          } else if (!line.startsWith(":")) {
            size += line.length;
            if (size > 1048576) throw new Error("SSE 单帧超出本机读取上限，已停止且未截断展示。");
            const separator = line.indexOf(":");
            const field = separator < 0 ? line : line.slice(0, separator);
            const value = separator < 0 ? "" : line.slice(separator + 1).replace(/^ /, "");
            if (field === "data") data.push(value);
            else if (field === "event") eventType = value;
            else if (field === "id") eventId = value;
          }
        }
        if (buffer.length + size > 1048576) throw new Error("SSE 单帧超出本机读取上限，已停止且未截断展示。");
      }
    } catch (failure) {
      assertReading(signal);
      if (failure instanceof RuntimeRequestError) throw failure;
      throw new Error("事件跟随中断或帧无效；请从已确认序号手动续读，不自动重试。");
    } finally {
      signal?.removeEventListener("abort", cancel);
      if (reader) { await reader.cancel().catch(() => {}); reader.releaseLock(); }
      else await response.body?.cancel().catch(() => {});
    }
  }
  async decide(runId: string, card: ApprovalCard, approve: boolean): Promise<void> {
    if (!card.call_id || !card.params_hash) throw new Error("缺少审批参数指纹，拒绝下发。");
    await this.request(`/v1/runs/${encodeURIComponent(runId)}/${approve ? "approve" : "reject"}`, "POST",
      { call_id: card.call_id, params_hash: card.params_hash });
  }
  async input(runId: string, card: InputCard, value?: unknown): Promise<void> {
    if (!["answer", "credential"].includes(card.kind)) throw new Error("未知写操作对账只读，不提供自动确认或重试。");
    await this.request(`/v1/runs/${encodeURIComponent(runId)}/input`, "POST",
      card.kind === "credential" ? { kind: "credential", call_id: card.call_id } : { kind: "answer", call_id: card.call_id, value });
  }
}
export function workbenchCards(run: WorkbenchRun, events: WorkbenchEvent[]): { approvals: ApprovalCard[]; input: InputCard | null } {
  const requested = new Map<string, WorkbenchEvent>(), decisions = new Map<string, WorkbenchEvent>(), settled = new Map<string, number>();
  let input: InputCard | null = null;
  for (const event of events) {
    const payload = event.payload, call = String(payload.call_id ?? "");
    if (event.type === "tool.requested") requested.set(call, event);
    if (event.type === "tool.decision") decisions.set(call, event);
    if (["tool.approved", "tool.rejected", "tool.result"].includes(event.type)) settled.set(call, event.seq);
    if (event.type === "input.requested") input = { kind: String(payload.kind), call_id: typeof payload.call_id === "string" ? payload.call_id : undefined,
      prompt: typeof payload.prompt === "string" ? payload.prompt : String(payload.reason ?? payload.kind), schema: payload.schema as InputCard["schema"] };
    if (event.type === "input.provided") input = null;
  }
  const approvals: ApprovalCard[] = [];
  if (run.state === "auth-required") for (const [call, event] of decisions) {
    const request = requested.get(call), payload = event.payload;
    if (payload.decision === "require_approval" && typeof payload.params_hash === "string" && request && (settled.get(call) ?? 0) < event.seq) {
      approvals.push({ call_id: call, params_hash: payload.params_hash, tool: String(request.payload.tool), input: request.payload.effective_input ?? request.payload.input });
    }
  }
  return { approvals, input: run.state === "input-required" ? input : null };
}
export function answerValue(card: InputCard, text: string): unknown {
  if (!text.trim()) throw new Error("请输入回答。");
  return card.schema ? JSON.parse(text) : text;
}
