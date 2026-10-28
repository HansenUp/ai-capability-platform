<script setup lang="ts">
import { computed } from "vue";
import { outcome, failureDetails, receiptGuidance } from "../run-outcome";
const props = defineProps<{ state: string; receipt?: { status: string; runId: string } | null; events: { seq: number; type: string; payload: Record<string, unknown> }[] }>();
const summary = computed(() => outcome(props.events));
const failures = computed(() => failureDetails(props.events));
</script>
<template>
  <section class="outcome-card" aria-label="任务结果摘要">
    <h4>任务结果摘要</h4>
    <p>Run 状态：{{ state }}。执行结束不等于业务成功。</p>
    <p v-if="summary.ok + summary.failed + summary.unknown">可见工具结果事件：成功 {{ summary.ok }} · 失败或拒绝 {{ summary.failed }} · 状态未知 {{ summary.unknown }}</p>
    <p v-else>工具结果：尚无可见结果，不能推断成功或失败。</p>
    <p v-if="summary.failed" role="alert">存在失败或拒绝记录；请查看对应工具结果，助手文本不作为成功摘要的证明。</p>
    <p>助手输出：{{ summary.texts ? '已有可见文本；是否形成有效业务摘要需核对内容。' : '尚无可见文本；不推断为已生成摘要。' }}</p>
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
