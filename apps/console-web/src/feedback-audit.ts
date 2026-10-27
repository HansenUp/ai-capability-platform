import { traceQuery, type ConsoleOperator } from "./operations";

export interface FeedbackAudit {
  scope: string; trust_mode: string; tenant_id: string; agent_id: string; subject: string; auth_method: string;
  since: string; until: string; read_started_at: string; read_finished_at: string; cohort_observed_at: string;
  complete_scan: boolean; snapshot_guaranteed: boolean; historical_snapshot_supported: boolean;
  counts: Record<"completed" | "failed" | "canceled" | "pending", number>;
  feedback: Record<"positive_runs" | "negative_runs" | "unverified_runs" | "non_binary_runs" | "invalid_score_runs" |
    "conflicting_runs" | "pre_terminal_runs" | "completed_runs" | "verified_feedback_runs", number> & {
      satisfaction: number | null; feedback_coverage: number | null; reason: string | null;
    };
}

export function feedbackQuery(agent: string, since: string, until: string): string {
  return traceQuery(agent, since, until).replace("/metrics/traces?", "/metrics/feedback?");
}

export function validateFeedback(value: unknown, tenant: string, path: string): FeedbackAudit {
  const row = value as FeedbackAudit, query = new URLSearchParams(path.split("?")[1]);
  if (!row || row.scope !== "local_feedback_audit_v1" || row.trust_mode !== "strict_readonly_unverified"
    || row.tenant_id !== tenant || row.agent_id !== query.get("agent_id") || row.auth_method !== "user"
    || typeof row.subject !== "string" || !row.subject.startsWith("human:") || row.subject.length <= 6
    || Date.parse(row.since) !== Date.parse(query.get("since") ?? "") || Date.parse(row.until) !== Date.parse(query.get("until") ?? "")
    || !Number.isFinite(Date.parse(row.read_started_at)) || !Number.isFinite(Date.parse(row.read_finished_at))
    || !Number.isFinite(Date.parse(row.cohort_observed_at)) || Date.parse(row.read_finished_at) < Date.parse(row.read_started_at)
    || row.complete_scan !== true || row.snapshot_guaranteed !== false || row.historical_snapshot_supported !== false) {
    throw new Error("反馈审计范围或来源契约不匹配。");
  }
  const fields = ["positive_runs", "negative_runs", "unverified_runs", "non_binary_runs", "invalid_score_runs", "conflicting_runs",
    "pre_terminal_runs", "completed_runs", "verified_feedback_runs"] as const;
  if (!row.counts || !row.feedback || ["completed", "failed", "canceled", "pending"].some(name =>
    !Number.isSafeInteger(row.counts[name as keyof typeof row.counts]) || row.counts[name as keyof typeof row.counts] < 0)
    || fields.some(name => !Number.isSafeInteger(row.feedback[name]) || row.feedback[name] < 0 || row.feedback[name] > row.counts.completed)
    || Object.values(row.counts).reduce((sum, count) => sum + count, 0) > 2000
    || row.feedback.completed_runs !== row.counts.completed || row.feedback.positive_runs !== 0 || row.feedback.negative_runs !== 0
    || row.feedback.verified_feedback_runs !== 0 || row.feedback.satisfaction !== null) throw new Error("反馈审计计数或可信度矛盾。");
  const blocked = row.feedback.unverified_runs > 0 || row.feedback.conflicting_runs > 0 || row.feedback.invalid_score_runs > 0;
  const coverage = row.counts.completed > 0 && !blocked ? 0 : null;
  const reason = blocked ? "UNVERIFIABLE_OR_CONFLICTING" : row.counts.completed === 0 ? "NO_SAMPLE" : "NO_BINARY_FEEDBACK";
  if (row.feedback.feedback_coverage !== coverage || row.feedback.reason !== reason) throw new Error("反馈覆盖率不可据缺失或冲突推断。");
  return row;
}

export async function readFeedback(path: string, operator: ConsoleOperator, token: string, signal: AbortSignal,
  development: boolean, fetcher: typeof fetch = fetch): Promise<FeedbackAudit> {
  if (!development || !path.startsWith("/metrics/feedback?") || !token || /\s/.test(token)) throw new Error("仅支持本机验签用户令牌只读查询。");
  if (signal.aborted) throw new DOMException("读取已取消", "AbortError");
  const transport = fetcher;
  const response = await transport(`/console${path}`, { method: "GET", cache: "no-store", redirect: "error", signal,
    headers: { Authorization: `Bearer ${token}`, "X-Console-User": operator.user, "X-Console-Role": operator.role, "X-Console-Tenant": operator.tenant } });
  if (signal.aborted) throw new DOMException("读取已取消", "AbortError");
  if (!response.ok) throw new Error(`HTTP ${response.status}：反馈审计不可用；不保留旧结果。`);
  const result = await response.json();
  if (signal.aborted) throw new DOMException("读取已取消", "AbortError");
  return validateFeedback(result, operator.tenant, path);
}
