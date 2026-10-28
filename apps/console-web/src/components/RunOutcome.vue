<script setup lang="ts">
import { computed } from "vue";
import { outcome } from "../run-outcome";
const props = defineProps<{ state: string; events: { seq: number; type: string; payload: Record<string, unknown> }[] }>();
const summary = computed(() => outcome(props.events));
</script>
<template>
  <section class="outcome-card" aria-label="任务结果摘要">
    <h4>任务结果摘要</h4>
    <p>Run 状态：{{ state }}。执行结束不等于业务成功。</p>
    <p v-if="summary.ok + summary.failed + summary.unknown">可见工具结果事件：成功 {{ summary.ok }} · 失败或拒绝 {{ summary.failed }} · 状态未知 {{ summary.unknown }}</p>
    <p v-else>工具结果：尚无可见结果，不能推断成功或失败。</p>
    <p v-if="summary.failed" role="alert">存在失败或拒绝记录；请查看对应工具结果，助手文本不作为成功摘要的证明。</p>
    <p>助手输出：{{ summary.texts ? '已有可见文本；是否形成有效业务摘要需核对内容。' : '尚无可见文本；不推断为已生成摘要。' }}</p>
    <p class="scope-note">仅归纳当前身份可见的事件，不计算成功率；缺失事件不补零为成功，不解除写锁。</p>
  </section>
</template>
<style scoped>
.outcome-card { border:1px solid #94a3b8; border-radius:10px; padding:16px; margin:12px 0; overflow-wrap:anywhere; }
h4 { margin-top:0; } .scope-note { color:#475569; font-size:.9em; }
</style>
