import { traceQuery, type ConsoleOperator } from "./operations";

export interface TrustedFeedbackAudit {
  scope: string; trust_mode: string; tenant_id: string; agent_id: string; subject: string; auth_method: string;
  since: string; until: string; read_started_at: string; read_finished_at: string; cohort_observed_at: string;
  complete_scan: boolean; snapshot_guaranteed: boolean; historical_snapshot_supported: boolean;
  counts: Record<"completed" | "failed" | "canceled" | "pending", number>;
  feedback: Record<"positive_runs" | "negative_runs" | "unverified_runs" | "neutral_runs" | "pending_runs" |
    "conflicting_runs" | "completed_runs" | "verified_feedback_runs", number> & {
      satisfaction: number | null; feedback_coverage: number | null; reason: string | null;
    };
}

export function trustedFeedbackQuery(agent: string, since: string, until: string): string {
  return traceQuery(agent, since, until).replace("/metrics/traces?", "/metrics/feedback-v2?");
}

export function validateTrustedFeedback(value: unknown, tenant: string, path: string): TrustedFeedbackAudit {
  const row = value as TrustedFeedbackAudit, query = new URLSearchParams(path.split("?")[1]);
  if (!row || row.scope !== "local_feedback_aggregate_v2" || row.trust_mode !== "runtime_observed_revision_v2"
    || row.tenant_id !== tenant || row.agent_id !== query.get("agent_id") || row.auth_method !== "user"
    || typeof row.subject !== "string" || !row.subject.startsWith("human:") || row.subject.length <= 6
    || Date.parse(row.since) !== Date.parse(query.get("since") ?? "") || Date.parse(row.until) !== Date.parse(query.get("until") ?? "")
    || !Number.isFinite(Date.parse(row.read_started_at)) || !Number.isFinite(Date.parse(row.read_finished_at))
    || !Number.isFinite(Date.parse(row.cohort_observed_at)) || Date.parse(row.read_finished_at) < Date.parse(row.read_started_at)
    || row.complete_scan !== true || row.snapshot_guaranteed !== false || row.historical_snapshot_supported !== false) {
    throw new Error("反馈审计范围或来源契约不匹配。");
  }
  const fields = ["positive_runs", "negative_runs", "unverified_runs", "neutral_runs", "pending_runs", "conflicting_runs",
    "completed_runs", "verified_feedback_runs"] as const;
  if (!row.counts || !row.feedback || ["completed", "failed", "canceled", "pending"].some(name =>
    !Number.isSafeInteger(row.counts[name as keyof typeof row.counts]) || row.counts[name as keyof typeof row.counts] < 0)
    || fields.some(name => !Number.isSafeInteger(row.feedback[name]) || row.feedback[name] < 0 || row.feedback[name] > row.counts.completed)
    || Object.values(row.counts).reduce((sum, count) => sum + count, 0) > 2000
    || row.feedback.completed_runs !== row.counts.completed || row.feedback.verified_feedback_runs !== row.feedback.positive_runs + row.feedback.negative_runs
    || row.feedback.verified_feedback_runs + row.feedback.neutral_runs + row.feedback.pending_runs + row.feedback.conflicting_runs > row.counts.completed) throw new Error("反馈审计计数或可信度矛盾。");
  const blocked = row.feedback.unverified_runs > 0 || row.feedback.conflicting_runs > 0 || row.feedback.pending_runs > 0;
  const binary = row.feedback.verified_feedback_runs;
  const coverage = row.counts.completed > 0 && !blocked ? binary / row.counts.completed : null;
  const satisfaction = binary > 0 && !blocked ? row.feedback.positive_runs / binary : null;
  const reason = blocked ? "INCOMPLETE_PROVENANCE" : row.counts.completed === 0 ? "NO_SAMPLE" : binary === 0 ? "NO_BINARY_FEEDBACK" : null;
  if (row.feedback.satisfaction !== satisfaction || row.feedback.feedback_coverage !== coverage || row.feedback.reason !== reason) throw new Error("反馈覆盖率不可据缺失或冲突推断。");
  return row;
}

export async function readTrustedFeedback(path: string, operator: ConsoleOperator, token: string, signal: AbortSignal,
  development: boolean, fetcher: typeof fetch = fetch): Promise<TrustedFeedbackAudit> {
  if (!development || !path.startsWith("/metrics/feedback-v2?") || !token || /\s/.test(token)) throw new Error("仅支持本机验签用户令牌只读查询。");
  if (signal.aborted) throw new DOMException("读取已取消", "AbortError");
  const transport = fetcher;
  const response = await transport(`/console${path}`, { method: "GET", cache: "no-store", redirect: "error", signal,
    headers: { Authorization: `Bearer ${token}`, "X-Console-User": operator.user, "X-Console-Role": operator.role, "X-Console-Tenant": operator.tenant } });
  if (signal.aborted) throw new DOMException("读取已取消", "AbortError");
  if (!response.ok) throw new Error(`HTTP ${response.status}：反馈审计不可用；不保留旧结果。`);
  const result = await response.json();
  if (signal.aborted) throw new DOMException("读取已取消", "AbortError");
  return validateTrustedFeedback(result, operator.tenant, path);
}
