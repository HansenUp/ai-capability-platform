export interface CommandOperator { user: string; role: string; tenant: string }
export interface CommandForm {
  node: string; type: string; pollInterval: string | number; timeout: string | number; restartTimeout: string | number;
  agent: string; base: string; expected: string; target: string; alias: string;
  skills: { name: string; version: string; sha: string }[];
}
export interface PreparedCommand {
  node: string; operator: CommandOperator; body: { type: string; args: Record<string, unknown> };
}
function number(value: string | number, minimum: number, maximum: number): number {
  const parsed = Number(value);
  if (!String(value).trim() || !Number.isFinite(parsed) || parsed < minimum || parsed > maximum) throw new Error(`数值范围应为 ${minimum}～${maximum}。`);
  return parsed;
}
function text(value: string, maximum: number): string {
  const result = value.trim();
  if (!result || result.length > maximum) throw new Error(`必填文本长度应为 1～${maximum}。`);
  return result;
}
export function prepareCommand(form: CommandForm, operator: CommandOperator, nodes: { node_id: string }[]): PreparedCommand {
  if (!["TENANT_ADMIN", "PLATFORM_ADMIN"].includes(operator.role)) throw new Error("需要租户管理员权限。");
  const node = text(form.node, 128);
  if (!nodes.some(item => item.node_id === node)) throw new Error("只能选择已读取的注册节点；请先刷新节点列表。");
  const args: Record<string, unknown> = {};
  if (form.type === "reload_config") {
    if (String(form.pollInterval).trim()) args.poll_interval_s = number(form.pollInterval, 1, 3600);
    if (String(form.timeout).trim()) args.timeout_s = number(form.timeout, 0.1, 120);
    if (!Object.keys(args).length) throw new Error("至少填写一个代理配置项。");
  } else if (form.type === "graceful_restart") {
    if (operator.role !== "PLATFORM_ADMIN") throw new Error("共享节点重启仅允许 PLATFORM_ADMIN。");
    args.timeout_s = number(form.restartTimeout, 0.1, 300);
  } else if (form.type === "switch_model") {
    Object.assign(args, { agent_id: text(form.agent, 64), base_version: text(form.base, 32),
      expected_version: text(form.expected, 32), target_version: text(form.target, 32), model_alias: text(form.alias, 64) });
  } else if (form.type === "reload_skills") {
    if (!form.skills.length) throw new Error("至少提供一个已签名 Skill 清单引用。");
    const seen = new Set<string>();
    args.skills = form.skills.map(skill => {
      const name = text(skill.name, 128), version = text(skill.version, 64), sha = skill.sha.trim();
      if (!/^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(name) || !/^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(version)
        || !/^[a-fA-F0-9]{64}$/.test(sha)) throw new Error("Skill 名称／版本必须为安全标识，清单 SHA256 必须是 64 位十六进制。");
      if (seen.has(name)) throw new Error("同一条指令不能重复声明 Skill。");
      seen.add(name);
      return { name, version, manifest_sha256: sha.toLowerCase() };
    });
  } else throw new Error("仅支持既有四类节点指令。");
  return { node, operator: { user: text(operator.user, 128), role: operator.role, tenant: text(operator.tenant, 64) }, body: { type: form.type, args } };
}

export class CommandSubmissionError extends Error {
  constructor(message: string, public readonly outcomeUnknown: boolean) { super(message); }
}
export async function submitCommand(prepared: PreparedCommand, development: boolean, fetcher: typeof fetch = fetch): Promise<string> {
  if (!development) throw new CommandSubmissionError("仅支持本机开发；生产统一鉴权尚未接入。", false);
  let response: Response;
  try {
    response = await fetcher(`/console/nodes/${encodeURIComponent(prepared.node)}/commands`, {
      method: "POST", cache: "no-store", redirect: "error", headers: { "Content-Type": "application/json", "X-Console-User": prepared.operator.user,
        "X-Console-Role": prepared.operator.role, "X-Console-Tenant": prepared.operator.tenant }, body: JSON.stringify(prepared.body),
    });
  } catch {
    throw new CommandSubmissionError("下发结果未知：可能已入队。禁止重发；请按原租户和节点核对指令审计，取得 ID 后只读查询回执。", true);
  }
  if (response.status !== 202) {
    const unknown = ![400, 401, 403, 404, 409, 422].includes(response.status);
    throw new CommandSubmissionError(`HTTP ${response.status}：${unknown ? "下发结果未知，禁止重发，请核对审计。" : "指令被拒绝，请检查权限和参数。"}`, unknown);
  }
  try {
    const result = await response.json();
    if (typeof result.id !== "string" || !result.id || result.id.length > 128) throw new Error("invalid id");
    return result.id;
  } catch {
    throw new CommandSubmissionError("已受理但指令 ID 无法读取：禁止重发，请核对审计后查询回执。", true);
  }
}
