<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from "vue";
import { metric, readSession, traceQuery } from "../operations";
import FeedbackAudit from "../components/FeedbackAudit.vue";
import TrustedFeedbackMetrics from "../components/TrustedFeedbackMetrics.vue";

interface ControlMetrics {
  tenant_id: string; scope: string; generated_at: string; agents: number; versions: Record<string, number>;
  pre_release_versions: number; assignment_relations: number; assigned_principals: number;
  agent_binding_records: number; commands: Record<string, number>; audit_events: number;
}
interface TraceMetrics {
  tenant_id: string; agent_id: string; scope: string; since: string; until: string; generated_at: string;
  trace_calls: number; generations: number; complete_scan: boolean; snapshot_guaranteed: boolean; run_metrics_available: boolean;
  tokens: { total: number | null; known_trace_subtotal: number; traces_with_complete_usage: number };
  cost_usd: { total: string | null; known_trace_subtotal: string; traces_with_complete_cost: number };
}
const operator = reactive({ user: "ops", role: "TENANT_ADMIN", tenant: "default" });
const query = reactive({ agent: "", since: "", until: "" });
const allowed = computed(() => ["TENANT_ADMIN", "PLATFORM_ADMIN"].includes(operator.role));
const control = ref<ControlMetrics | null>(null), traces = ref<TraceMetrics | null>(null);
const controlError = ref(""), traceError = ref("");
const controlBusy = ref(false), traceBusy = ref(false);
const controlRead = readSession(import.meta.env.DEV), traceRead = readSession(import.meta.env.DEV);
let controlGeneration = 0, traceGeneration = 0;

function clearTraces() {
  traceGeneration += 1; traceRead.invalidate(); traces.value = null; traceError.value = ""; traceBusy.value = false;
}
watch(query, clearTraces, { flush: "sync" });
watch(operator, () => {
  controlGeneration += 1; controlRead.invalidate(); control.value = null; controlError.value = ""; controlBusy.value = false;
  clearTraces();
}, { flush: "sync" });
async function loadControl() {
  const current = ++controlGeneration;
  control.value = null; controlError.value = ""; controlBusy.value = true;
  try {
    const result = await controlRead.read<ControlMetrics>("/metrics/operations", { ...operator });
    if (current !== controlGeneration) return;
    if (result.tenant_id !== operator.tenant || result.scope !== "console_control_plane") throw new Error("控制面指标范围不匹配。");
    control.value = result;
  } catch (error) { if (current === controlGeneration) controlError.value = String(error); }
  finally { if (current === controlGeneration) controlBusy.value = false; }
}
async function loadTraces() {
  clearTraces();
  const current = traceGeneration;
  traceBusy.value = true;
  try {
    const path = traceQuery(query.agent, query.since, query.until);
    const expected = new URLSearchParams(path.split("?")[1]);
    const result = await traceRead.read<TraceMetrics>(path, { ...operator });
    if (current !== traceGeneration) return;
    if (Date.parse(result.since) !== Date.parse(expected.get("since") ?? "") || Date.parse(result.until) !== Date.parse(expected.get("until") ?? ""))
      throw new Error("Trace 指标时间窗不匹配。");
    if (result.tenant_id !== operator.tenant || result.agent_id !== query.agent.trim() || result.scope !== "local_langfuse_v3_trace_generations"
      || result.complete_scan !== true || result.run_metrics_available !== false) throw new Error("Trace 指标范围或完整性不匹配。");
    traces.value = result;
  } catch (error) { if (current === traceGeneration) traceError.value = String(error); }
  finally { if (current === traceGeneration) traceBusy.value = false; }
}
onBeforeUnmount(() => { controlRead.invalidate(); traceRead.invalidate(); });
</script>

<template>
  <section>
    <h2>运营只读视图</h2>
    <p>复用 S10 控制面与本机 Langfuse 接口，不新增采集或写入。Trace 不等于 Run；费用是来源报告的 USD，不是财务结算。</p>
    <fieldset><legend>本机开发身份（非生产鉴权）</legend>
      <label>用户 <input v-model="operator.user" maxlength="128" /></label>
      <label>角色 <select v-model="operator.role"><option v-for="role in ['USER', 'ADMIN', 'TENANT_ADMIN', 'PLATFORM_ADMIN']" :key="role">{{ role }}</option></select></label>
      <label>租户 <input v-model="operator.tenant" maxlength="64" /></label>
    </fieldset>
    <p v-if="!allowed">需要租户管理员权限；切换身份会清空已有数据。</p>
    <h3>当前租户控制面计数</h3>
    <button :disabled="!allowed || controlBusy" @click="loadControl">{{ controlBusy ? '读取中…' : '刷新控制面' }}</button>
    <p v-if="controlError" role="alert">{{ controlError }}</p>
    <div v-if="control">
      <p>租户 {{ control.tenant_id }} · 读取时间 {{ control.generated_at }} · 非历史时间窗统计</p>
      <dl><dt>Agent</dt><dd>{{ metric(control.agents) }}</dd><dt>预发布版本（0%）</dt><dd>{{ metric(control.pre_release_versions) }}</dd>
        <dt>分配关系 / 已分配主体</dt><dd>{{ metric(control.assignment_relations) }} / {{ metric(control.assigned_principals) }}</dd>
        <dt>路由绑定记录 / 审计事件</dt><dd>{{ metric(control.agent_binding_records) }} / {{ metric(control.audit_events) }}</dd></dl>
      <p>版本状态计数</p><ul><li v-for="(count, status) in control.versions" :key="status">{{ status }}：{{ metric(count) }}</li></ul>
      <p>指令状态计数（不是 Run 成功率）</p><ul><li v-for="(count, status) in control.commands" :key="status">{{ status }}：{{ metric(count) }}</li></ul>
    </div>
    <p v-else>控制面数据未读取或不可用。</p>
    <h3>本机 Langfuse Trace 用量与费用</h3>
    <form @submit.prevent="loadTraces"><fieldset :disabled="!allowed || traceBusy"><legend>同租户 Agent · 最长七天</legend>
      <label>Agent ID <input v-model="query.agent" required maxlength="64" /></label>
      <label>开始（浏览器本地时间）<input v-model="query.since" type="datetime-local" required /></label>
      <label>结束（浏览器本地时间）<input v-model="query.until" type="datetime-local" required /></label>
      <button>{{ traceBusy ? '读取中…' : '查询 Trace 指标' }}</button>
    </fieldset></form>
    <p v-if="traceError" role="alert">{{ traceError }}</p>
    <div v-if="traces">
      <p>租户 {{ traces.tenant_id }} / {{ traces.agent_id }} · {{ traces.since }} 至 {{ traces.until }} · 读取时间 {{ traces.generated_at }}</p>
      <dl><dt>Trace 调用 / Generation</dt><dd>{{ metric(traces.trace_calls) }} / {{ metric(traces.generations) }}</dd>
        <dt>Token 总量</dt><dd>{{ metric(traces.tokens.total) }}</dd>
        <dt>Token 已知小计 / 用量完整 Trace</dt><dd>{{ metric(traces.tokens.known_trace_subtotal) }} / {{ metric(traces.tokens.traces_with_complete_usage) }}</dd>
        <dt>费用总量（USD）</dt><dd>{{ metric(traces.cost_usd.total) }}</dd>
        <dt>费用已知小计（USD）/ 费用完整 Trace</dt><dd>{{ metric(traces.cost_usd.known_trace_subtotal) }} / {{ metric(traces.cost_usd.traces_with_complete_cost) }}</dd></dl>
      <p>已完成有界扫描；不保证并发写入时的快照。已知小计不是总量；缺失值显示未知，不补零。</p>
    </div>
    <p v-else>Trace 数据尚未读取或不可用；不显示上一次结果。</p>
    <FeedbackAudit :operator="operator" />
    <TrustedFeedbackMetrics :operator="operator" />
    <p>待定义：Run 成功率、时延、反馈采纳率及北极星指标；本页不推断这些值。</p>
  </section>
</template>

<style scoped>
fieldset { margin: 16px 0; padding: 12px; } label { display: inline-flex; gap: 6px; margin: 6px; }
dl { display: grid; grid-template-columns: minmax(180px, 1fr) 1fr; gap: 8px; } dd { margin: 0; }
input, select, button { padding: 6px; } [role="alert"] { color: #b91c1c; } p { overflow-wrap: anywhere; }
</style>
