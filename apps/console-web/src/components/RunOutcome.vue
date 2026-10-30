<script setup lang="ts">
import OrderMetrics from "./OrderMetrics.vue";
import { computed } from "vue";
import { outcome, failureDetails, receiptGuidance, usageDetails, budgetDetails, usageCoverage } from "../run-outcome";
const props = defineProps<{ state: string; receipt?: { status: string; runId: string } | null; events: { seq: number; type: string; payload: Record<string, unknown> }[] }>();
const tables = computed(() => {
  const calls = new Set(props.events.filter(e => e.type === "tool.requested" && e.payload.tool === "data.query").map(e => e.payload.call_id));
  return props.events.flatMap(e => {
    const o = e.payload.output as { columns?: unknown; rows?: unknown; view_version?: unknown; truncated?: unknown } | undefined;
    if (e.type !== "tool.result" || e.payload.status !== "ok" || !calls.has(e.payload.call_id) || !o
      || !Array.isArray(o.columns) || !o.columns.every(c => typeof c === "string") || !Array.isArray(o.rows) || o.rows.length > 100
      || !o.rows.every(r => Array.isArray(r) && r.length === (o.columns as unknown[]).length && r.every(v => v === null || typeof v === "string" || (typeof v === "number" && Number.isFinite(v))))
      || typeof o.view_version !== "string" || typeof o.truncated !== "boolean") return [];
    return [{ seq: e.seq, columns: o.columns as string[], rows: o.rows as (string | number | null)[][], version: o.view_version, truncated: o.truncated }];
  });
});
const summary = computed(() => outcome(props.events));
const coverage = computed(() => usageCoverage(props.events));
const usage = computed(() => usageDetails(props.events));
const budgets = computed(() => budgetDetails(props.events));
const failures = computed(() => failureDetails(props.events));
</script>
<template>
  <section class="outcome-card" aria-label="任务结果摘要">
    <h4>任务结果摘要</h4>
    <OrderMetrics :events="events" />
    <section v-for="table in tables" :key="table.seq" aria-label="受限查询结果">
      <h4>受限查询结果 · {{ table.version }}</h4><p>{{ table.rows.length }} 行；{{ table.truncated ? '已截断，不代表全量' : '本次限行内未截断' }}。仅来自同 Run 工具回执，签名下载暂缓。</p>
      <table><thead><tr><th v-for="column in table.columns" :key="column">{{ column }}</th></tr></thead><tbody><tr v-for="(row, index) in table.rows" :key="index"><td v-for="(cell, j) in row" :key="j">{{ cell === null ? 'NULL' : cell }}</td></tr></tbody></table>
    </section>
    <p>Run 状态：{{ state }}。执行结束不等于业务成功。</p>
    <p v-if="summary.ok + summary.failed + summary.unknown">可见工具结果事件：成功 {{ summary.ok }} · 失败或拒绝 {{ summary.failed }} · 状态未知 {{ summary.unknown }}</p>
    <p v-else>工具结果：尚无可见结果，不能推断成功或失败。</p>
    <p v-if="summary.failed" role="alert">存在失败或拒绝记录；请查看对应工具结果，助手文本不作为成功摘要的证明。</p>
    <p>助手输出：{{ summary.texts ? '已有可见文本；是否形成有效业务摘要需核对内容。' : '尚无可见文本；不推断为已生成摘要。' }}</p>
    <section aria-label="模型用量与预算">
      <h4>模型用量与预算</h4>
      <p>来源：当前身份可见的同 Run 主执行模型响应事件（含工具编排轮次）。记录值不等于供应商账单，不代表全部模型消耗。</p>
      <ul v-if="usage.length">
        <li v-for="row in usage" :key="row.seq">事件 #{{ row.seq }} · 模型 {{ row.model }} · 输入 Token：{{ row.input ?? '未知' }} · 输出 Token：{{ row.output ?? '未知' }} · 成本记录：{{ row.recordedCost ?? '未知' }}（币种与计价来源未提供）</li>
      </ul>
      <p v-else>Token 与模型：未知，尚无可见模型响应记录。</p>
      <section aria-label="用量统计覆盖范围">
        <h5>来源分类与统计覆盖范围</h5>
        <p>主执行：上方逐事件记录仅覆盖可见 model.response，模型标识不能证明是否发生过回退。</p>
        <p>思维摘要：{{ coverage.summaries.length ? '存在摘要结果事件 #' + coverage.summaries.join('、#') : '没有可见摘要结果证据' }}；无法从结果区分本地截断或模型生成，独立调用 Token 与费用未知。</p>
        <ul v-if="coverage.summaryRecords.length"><li v-for="row in coverage.summaryRecords" :key="`${row.seq}-${row.index}`">思维摘要响应记录 #{{ row.seq }}：模型 {{ row.record.model }}，输入 Token {{ row.record.input ?? '未知' }}，输出 Token {{ row.record.output ?? '未知' }}，成本记录 {{ row.record.recordedCost ?? '未知' }}；可能含默认0，币种及实付未知，不含失败尝试。</li></ul>
        <ul v-if="coverage.compactions.length"><li v-for="row in coverage.compactions" :key="row.seq">上下文压缩事件 #{{ row.seq }}：{{ row.method }}。<span v-if="row.record">独立响应记录：模型 {{ row.record.model }}，输入 Token {{ row.record.input ?? '未知' }}，输出 Token {{ row.record.output ?? '未知' }}，成本记录 {{ row.record.recordedCost ?? '未知' }}；缺失可能默认0，币种及实付未知，不含失败尝试。</span><span v-else>独立调用 Token 与费用未知。</span></li></ul>
        <p v-else>上下文压缩：没有可见压缩事件，不证明未尝试调用。</p>
        <p>压缩前后 tokens 是上下文大小估计，不是供应商计费用量，不计入上方记录。</p>
        <p>回退与重试：缺少逐次调用记录，次数和消耗未知；主模型响应不覆盖所有尝试。</p>
        <p>覆盖范围不完整：不提供全 Run 或账户总消费，不读取摘要正文或推理内容。</p>
      </section>
      <p>实际费用：未知。旧记录可能将缺失用量或费用默认写为 0；不能据此认定免费或没有调用，不汇总为账户消费。</p>
      <ul v-if="budgets.length"><li v-for="row in budgets" :key="row.seq">事件 #{{ row.seq }} · {{ row.kind }}预算已触限：已用 {{ row.used ?? '未知' }} / 限额 {{ row.limit ?? '未知' }}。</li></ul>
      <p v-else>运行预算状态：未知；当前事件没有超限证据，不代表尚有额度。</p>
      <p>人民币预算及剩余额度：未知。Token／时长限制不是收费硬上限，此处不恢复调用或解除未决写锁。</p>
    </section>
    <section aria-label="失败原因与只读恢复">
      <h4>失败原因与只读恢复</h4>
      <ul v-if="failures.length"><li v-for="failure in failures" :key="failure.seq">事件 #{{ failure.seq }} · {{ failure.kind }}：{{ failure.reason }}</li></ul>
      <p v-else>{{ state === 'failed' ? 'Run 已失败，但当前可见事件未提供可归类的原因；不猜测根因。' : '当前可见事件未提供可归类的失败原因；不作为成功证明。' }}</p>
      <p v-if="receipt">当前主体活动回执：{{ receipt.status }} · Run {{ receipt.runId }}（可能属于其他任务）。</p>
      <p>{{ receiptGuidance(receipt?.status) }}</p>
      <ol><li>凭据过期或身份变化：重新认证并显式应用身份，旧结果不沿用。</li><li>定位原 Run，显式只读读取状态、事件和账本；断连后可手动续读持久事件。</li><li>结果未知时保持锁定，不重新执行。Run终态、SSE结束、404或空列表都不是解锁证据。</li></ol>
      <p>这里不提供重试、执行或确认收口按钮；原始错误正文按原权限在轨迹中展开查看。</p>
    </section>
    <p class="scope-note">仅归纳当前身份可见的事件，不计算成功率；缺失事件不补零为成功，不解除写锁。</p>
  </section>
</template>
<style scoped>
.outcome-card { border:1px solid #94a3b8; border-radius:10px; padding:16px; margin:12px 0; overflow-wrap:anywhere; }
h4 { margin-top:0; } .scope-note { color:#475569; font-size:.9em; }
</style>
