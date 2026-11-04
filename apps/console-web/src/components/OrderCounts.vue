<script setup lang="ts">
import {computed} from "vue";
import {orderCountResults} from "../order-count-result";
const props = defineProps<{events:{seq:number;type:string;payload:Record<string,unknown>}[]}>();
const rows = computed(() => orderCountResults(props.events));
const labels = {all:"全部",pending:"待完成",completed:"已完成",cancelled:"已取消"};
</script>
<template>
  <section v-for="row in rows" :key="row.seq" aria-label="订单数量结果">
    <h4>订单数量 · {{labels[row.value.status_filter]}}</h4>
    <p>创建窗口 [{{row.value.since}}, {{row.value.until}})；状态为读取时当前状态，非历史快照。</p>
    <p>视图 {{row.value.view_version}} · 策略 {{row.value.policy_version}} · 读取 {{row.value.read_started}} — {{row.value.read_finished}}</p>
    <template v-if="row.value.complete"><p>所选范围订单数：{{row.value.order_count}}</p>
      <p v-if="row.value.status_filter === 'all'">待完成 {{row.value.statuses?.pending}} / 已完成 {{row.value.statuses?.completed}} / 已取消 {{row.value.statuses?.cancelled}}</p>
    </template>
    <p v-else role="alert">统计不可用：{{row.value.reason}}；数量未知，不补零。</p>
    <p>仅来自同Run匹配工具回执；不显示明细或金额，不代表收入，不跨租户合计。未知结果只读恢复，不自动重试。</p>
  </section>
</template>
