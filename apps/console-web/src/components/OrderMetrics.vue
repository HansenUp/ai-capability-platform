<script setup lang="ts">
import {computed} from "vue";
import {orderMetrics} from "../order-metrics";
const props=defineProps<{events:{seq:number;type:string;payload:Record<string,unknown>}[]}>();
const rows=computed(()=>orderMetrics(props.events));
</script>
<template><section v-for="row in rows" :key="row.seq" aria-label="经营指标">
<h4>合成订单经营指标</h4><p>创建窗口 {{row.value.since}} 至 {{row.value.until}}（左闭右开）。读取时当前状态，非历史快照，不是营业收入或利润。</p>
<p>视图 {{row.value.view_version}} · 策略 {{row.value.policy_version}} · 读取 {{row.value.read_started}} — {{row.value.read_finished}}</p>
<p v-if="!row.value.complete" role="alert">统计不可用：{{row.value.reason}}，不提供正式总量。</p>
<template v-else><p>订单数：{{row.value.order_count}}；完成 {{row.value.statuses.completed}} / 待完成 {{row.value.statuses.pending}} / 取消 {{row.value.statuses.cancelled}}</p>
<p v-if="row.value.reason" role="alert">{{row.value.reason}}</p><p>更新时间证据未知：{{row.value.unknown_updated}}。迟到数据或状态变化可改变重查结果。</p>
<table><thead><tr><th>币种</th><th>完成订单金额（分）</th><th>已知小计（分）</th><th>金额覆盖率</th></tr></thead><tbody><tr v-for="g in row.value.currencies" :key="g.currency"><td>{{g.currency}}</td><td>{{g.total_minor ?? '未知'}}</td><td>{{g.known_minor ?? '未知'}}</td><td>{{g.valid}} / {{g.completed}}（{{g.coverage}}）</td></tr></tbody></table>
<p v-if="!row.value.currencies.length">无可用金额组；覆盖率未知，不补零。</p></template>
<p>仅同Run权威工具结果；不跨币种合计、不下载、不自动重试或解锁。</p>
</section></template>
