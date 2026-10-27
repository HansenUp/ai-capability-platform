<script setup lang="ts">
import { inject, onBeforeUnmount, reactive, ref, watch } from "vue";
import { metric } from "../operations";
import { ownerSessionKey } from "../owner-session";

interface Summary { versions: { draft: number; published: number; retired: number }; tenant_id: string; read_finished_at: string }
interface TraceMetrics {
  tenant_id: string; read_finished_at: string; trace_calls: number; generations: number;
  tokens: { total: number | null; known_trace_subtotal: number; traces_with_complete_usage: number };
  cost_usd: { total: string | null; known_trace_subtotal: string; traces_with_complete_cost: number };
}
const development = import.meta.env.DEV;
const connection = development ? inject(ownerSessionKey, null) : null;
const sessionIdentity = ref<{ tenant: string; subject: string } | null>(null), loginStatus = ref("");
const draft = ref(""), token = ref("");
const query = reactive({ agent: "", since: "", until: "" });
const summary = ref<Summary | null>(null), traces = ref<TraceMetrics | null>(null);
const busy = reactive({ summary: false, traces: false }), errors = reactive({ summary: "", traces: "" });
const controllers = { summary: null as AbortController | null, traces: null as AbortController | null };
const sequences = { summary: 0, traces: 0 };
let generation = 0;
let loginSequence = 0;
function clear() {
  generation += 1;
  for (const key of ["summary", "traces"] as const) { controllers[key]?.abort(); controllers[key] = null; busy[key] = false; errors[key] = ""; }
  summary.value = null; traces.value = null;
}
watch(query, clear, { flush: "sync" });
watch(draft, () => { token.value = ""; clear(); }, { flush: "sync" });
const unsubscribe = connection?.session.subscribe(view => {
  clear(); token.value = view.token; sessionIdentity.value = view.identity;
});
onBeforeUnmount(() => { loginSequence += 1; unsubscribe?.(); connection?.session.dispose(); token.value = ""; draft.value = ""; clear(); });
async function login() {
  if (!connection) return;
  const sequence = ++loginSequence;
  loginStatus.value = "";
  try { const url = await connection.session.begin(); if (sequence !== loginSequence) return;
    connection.authorize(url); loginStatus.value = "测试登录事务已准备，等待替身回调。"; }
  catch { if (sequence === loginSequence) loginStatus.value = "Owner登录未完成。"; }
}
function logout() { loginSequence += 1; loginStatus.value = ""; connection?.session.logout(); }
function apply() {
  const value = draft.value.trim();
  draft.value = ""; clear(); token.value = value;
}
function count(value: unknown): value is number { return typeof value === "number" && Number.isSafeInteger(value) && value >= 0; }
function usd(value: unknown): string { return typeof value === "string" && /^\d+(\.\d+)?([eE][+-]?\d+)?$/.test(value) ? value : "未知"; }
async function load(key: "summary" | "traces") {
  if (connection) token.value = connection.session.snapshot().token;
  if (!development || !token.value || !query.agent.trim()) return;
  const current = generation, sequence = ++sequences[key];
  controllers[key]?.abort(); controllers[key] = new AbortController();
  if (key === "summary") summary.value = null; else traces.value = null;
  busy[key] = true; errors[key] = "";
  try {
    let suffix = "summary", expectedSince = "", expectedUntil = "";
    if (key === "traces") {
      const since = new Date(query.since), until = new Date(query.until);
      if (!Number.isFinite(since.getTime()) || !Number.isFinite(until.getTime()) || until <= since || +until - +since > 7 * 86400000) {
        throw new Error("请选择有效时间窗（最长七天）。");
      }
      expectedSince = since.toISOString(); expectedUntil = until.toISOString();
      suffix = `trace-metrics?${new URLSearchParams({ since: expectedSince, until: expectedUntil })}`;
    }
    const response = await fetch(`/console/owner/agents/${encodeURIComponent(query.agent.trim())}/${suffix}`, {
      method: "GET", cache: "no-store", signal: controllers[key]!.signal, headers: { Authorization: `Bearer ${token.value}` },
    });
    connection?.session.snapshot();
    if (current !== generation || sequence !== sequences[key]) return;
    if (!response.ok) {
      const message = `HTTP ${response.status}：Owner数据不可用。`;
      if ([401, 403, 404].includes(response.status)) { connection?.session.logout(); clear(); token.value = ""; errors[key] = message; return; }
      throw new Error(message);
    }
    const body = await response.json();
    connection?.session.snapshot();
    if (current !== generation || sequence !== sequences[key]) return;
    const expectedScope = key === "summary" ? "owner_agent_summary_v1" : "owner_agent_trace_metrics_v1";
    if (body.scope !== expectedScope || body.schema_version !== 1 || body.agent_id !== query.agent.trim()
      || typeof body.tenant_id !== "string" || !body.tenant_id || body.snapshot_guaranteed !== false
      || body.authorization_policy !== "current_assignment_rechecked_v1" || !Number.isFinite(Date.parse(body.read_finished_at))) {
      throw new Error("Owner响应范围不匹配。");
    }
    const other = key === "summary" ? traces.value : summary.value;
    if ((other && other.tenant_id !== body.tenant_id) || (sessionIdentity.value && sessionIdentity.value.tenant !== body.tenant_id)) {
      connection?.session.logout(); clear(); token.value = ""; errors[key] = "Owner响应租户不一致。"; return;
    }
    if (key === "summary") {
      if (!body.versions || ["draft", "published", "retired"].some(status => !Number.isSafeInteger(body.versions[status]) || body.versions[status] < 0)) {
        throw new Error("Owner摘要无效。");
      }
      summary.value = body;
    } else {
      if (body.interval !== "half_open" || body.complete_scan !== true || body.run_metrics_available !== false
        || body.source_trust !== "metadata_consistency_only" || Date.parse(body.since) !== Date.parse(expectedSince)
        || Date.parse(body.until) !== Date.parse(expectedUntil) || !body.tokens || !body.cost_usd
        || !count(body.trace_calls) || !count(body.generations) || !count(body.tokens.known_trace_subtotal)
        || !count(body.tokens.traces_with_complete_usage) || body.tokens.traces_with_complete_usage > body.trace_calls
        || (body.tokens.total !== null && !count(body.tokens.total))
        || !count(body.cost_usd.traces_with_complete_cost) || body.cost_usd.traces_with_complete_cost > body.trace_calls
        || usd(body.cost_usd.known_trace_subtotal) === "未知" || (body.cost_usd.total !== null && usd(body.cost_usd.total) === "未知")) {
        throw new Error("Owner指标无效。");
      }
      traces.value = body;
    }
  } catch (error) {
    if (current === generation && sequence === sequences[key]) errors[key] = error instanceof Error ? error.message : "Owner读取失败。";
  } finally { if (current === generation && sequence === sequences[key]) busy[key] = false; }
}
</script>

<template>
  <h2>Owner单Agent只读运营</h2>
  <p>仅本机隔离测试profile；不包含反馈或原始Trace详情，不增加现有管理员权限。未配置profile时不可用。</p>
  <p v-if="!development">生产环境不启用此本机入口。</p>
  <section v-if="connection"><h3>OIDC隔离会话（纯替身）</h3>
    <button @click="login">发起Owner测试登录</button><button @click="logout">退出Owner测试会话</button>
    <p role="status">{{ loginStatus }}</p>
    <p v-if="sessionIdentity">测试登录主体 {{ sessionIdentity.subject }} · 租户 {{ sessionIdentity.tenant }}；API仍须服务端验签和分配。</p>
  </section>
  <fieldset v-else :disabled="!development"><legend>签名测试身份（仅内存）</legend>
    <label>Owner用户令牌 <input v-model="draft" type="password" autocomplete="off"></label>
    <button @click="apply">应用Owner身份并清空</button>
    <p>{{ token ? '已应用测试身份；仍须服务端验签和当前分配核对。' : '尚未应用Owner身份。' }}</p>
  </fieldset>
  <label>Owner Agent ID <input v-model="query.agent" maxlength="64"></label>
  <button :disabled="!token || !query.agent.trim() || busy.summary" @click="load('summary')">查询Owner摘要</button>
  <p v-if="errors.summary" role="alert">{{ errors.summary }}</p>
  <section v-if="summary"><h3>Owner摘要</h3><p>租户 {{ summary.tenant_id }} · 读取时间 {{ summary.read_finished_at }}</p>
    <dl><dt>草稿版本</dt><dd>{{ summary.versions.draft }}</dd><dt>发布版本</dt><dd>{{ summary.versions.published }}</dd><dt>退役版本</dt><dd>{{ summary.versions.retired }}</dd></dl>
  </section>
  <form @submit.prevent="load('traces')"><fieldset :disabled="!development || !token || busy.traces"><legend>Owner Trace半开时间窗</legend>
    <label>Owner开始时间 <input v-model="query.since" type="datetime-local" required></label>
    <label>Owner结束时间 <input v-model="query.until" type="datetime-local" required></label>
    <button :disabled="!query.agent.trim()">查询Owner Trace</button>
  </fieldset></form>
  <p v-if="errors.traces" role="alert">{{ errors.traces }}</p>
  <section v-if="traces"><h3>Owner Trace聚合</h3><p>租户 {{ traces.tenant_id }} · 读取时间 {{ traces.read_finished_at }}</p>
    <dl><dt>Trace / Generation</dt><dd>{{ traces.trace_calls }} / {{ traces.generations }}</dd>
      <dt>Token总量</dt><dd>{{ metric(traces.tokens.total) }}</dd><dt>Token已知小计 / 完整Trace数</dt><dd>{{ metric(traces.tokens.known_trace_subtotal) }} / {{ traces.tokens.traces_with_complete_usage }}</dd>
      <dt>来源费用USD</dt><dd>{{ usd(traces.cost_usd.total) }}</dd><dt>费用已知小计 / 完整Trace数</dt><dd>{{ usd(traces.cost_usd.known_trace_subtotal) }} / {{ traces.cost_usd.traces_with_complete_cost }}</dd></dl>
  </section>
  <p>当前分配者可读接手前的指定窗口聚合；Trace不是Run，费用不是结算。缺失值未知，已知小计不是总量；非快照，元数据一致不等于可信来源证明。</p>
  <p>最终授权核对后已准入响应可完成；不保证即时撤权或清除其他设备已收到的数据。北极星仍待定义。</p>
</template>
