<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from "vue";
import { trustedFeedbackQuery, readTrustedFeedback, type TrustedFeedbackAudit } from "../trusted-feedback-metrics";
import type { ConsoleOperator } from "../operations";

const props = defineProps<{ operator: ConsoleOperator }>();
const query = reactive({ agent: "", since: "", until: "" });
const tokenDraft = ref(""), token = ref(""), busy = ref(false), error = ref("");
const result = ref<TrustedFeedbackAudit | null>(null);
const allowed = computed(() => ["TENANT_ADMIN", "PLATFORM_ADMIN"].includes(props.operator.role));
let generation = 0, controller: AbortController | null = null;
function clear() { generation++; controller?.abort(); controller = null; result.value = null; error.value = ""; busy.value = false; }
watch(query, clear, { flush: "sync" });
watch(tokenDraft, clear, { flush: "sync" });
watch(() => props.operator, () => { clear(); token.value = ""; tokenDraft.value = ""; }, { deep: true, flush: "sync" });
function apply() { clear(); token.value = tokenDraft.value.trim(); tokenDraft.value = ""; }
async function load() {
  clear(); const current = generation;
  if (!allowed.value || !token.value) return;
  const reading = new AbortController(); controller = reading; busy.value = true;
  try {
    const path = trustedFeedbackQuery(query.agent, query.since, query.until);
    const value = await readTrustedFeedback(path, { ...props.operator }, token.value, reading.signal, import.meta.env.DEV);
    if (current === generation) result.value = value;
  } catch (failure) { if (current === generation) error.value = String(failure); }
  finally { if (current === generation) { busy.value = false; controller = null; } }
}
onBeforeUnmount(() => { clear(); token.value = ""; tokenDraft.value = ""; });
function percent(value: number | null) { return value === null ? "未知" : `${(value * 100).toFixed(2)}%`; }
</script>

<template>
  <section aria-label="可信反馈聚合 v2">
    <h3>单租户可信反馈聚合 v2</h3>
    <p>按创建时间 [since, until) 选取 Run，分母仅为读取时已完成 Run。满意度不是采纳率；北极星仍待定义。</p>
    <p>仅接受验签用户的 platform-admin 权限。Console 开发身份不是反馈分母的权威身份；令牌仅内存保存，不使用发布服务账号。</p>
    <label>v2 读取令牌（验签平台管理员）<input v-model="tokenDraft" type="password" autocomplete="off" /></label>
    <button :disabled="!allowed" @click="apply">应用 v2 读取身份</button>
    <p v-if="token">已应用读取令牌；修改草稿后需重新应用。切换 Console 身份将清空令牌。</p>
    <form @submit.prevent="load"><fieldset :disabled="!allowed || !token || busy"><legend>反馈创建时间窗 · 最长七天</legend>
      <label>v2 Agent ID <input v-model="query.agent" required maxlength="64" /></label>
      <label>v2 开始（浏览器本地时间）<input v-model="query.since" type="datetime-local" required /></label>
      <label>v2 结束（浏览器本地时间）<input v-model="query.until" type="datetime-local" required /></label>
      <button>{{ busy ? '反馈读取中…' : '查询可信反馈 v2' }}</button>
    </fieldset></form>
    <p v-if="error" role="alert">{{ error }}</p>
    <section v-if="result" aria-label="可信反馈 v2 结果">
      <p>实际验签身份：{{ result.tenant_id }} / {{ result.subject }} · Agent {{ result.agent_id }}</p>
      <p>{{ result.since }} 至 {{ result.until }}（右端不含）；读取 {{ result.read_started_at }} 至 {{ result.read_finished_at }}。</p>
      <dl><dt>已完成 Run 分母</dt><dd>{{ result.counts.completed }}</dd><dt>失败 / 取消 / 未决</dt><dd>{{ result.counts.failed }} / {{ result.counts.canceled }} / {{ result.counts.pending }}</dd>
        <dt>已验证点赞 / 点踩 Run</dt><dd>{{ result.feedback.positive_runs }} / {{ result.feedback.negative_runs }}</dd>
        <dt>可信满意度</dt><dd>{{ percent(result.feedback.satisfaction) }}</dd>
        <dt>有效二元反馈覆盖率</dt><dd>{{ percent(result.feedback.feedback_coverage) }} · {{ result.feedback.verified_feedback_runs }} / {{ result.counts.completed }}</dd>
        <dt>未验证来源 Run</dt><dd>{{ result.feedback.unverified_runs }}</dd><dt>最新中性 / 最新未决 Run</dt><dd>{{ result.feedback.neutral_runs }} / {{ result.feedback.pending_runs }}</dd>
        <dt>来源冲突 Run</dt><dd>{{ result.feedback.conflicting_runs }}</dd></dl>
      <p>可信度：Runtime OBSERVED 摘要与当前评分、Trace 在线核验；只按服务端最新修订。未验证历史隔离，不会升级信任；混合未验证来源时整体比率未知。原因：{{ result.feedback.reason ?? '无' }}。</p>
      <p>诊断数量可重叠，不可相加作为总反馈数。最新中性、未决、缺失或冲突均不回溯旧点赞／点踩。已验证计数仅是已知诊断，完整性不足时不是整体满意度。不支持历史快照；迟到反馈和并发写入可能改变结果。</p>
    </section>
    <p v-else>反馈尚未读取或不可用；不显示上一次结果，不推断为零。</p>
  </section>
</template>
