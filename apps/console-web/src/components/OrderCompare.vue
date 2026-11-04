<script setup lang="ts">
import {computed} from "vue";
import {orderCompareResults} from "../order-compare-result";
const props=defineProps<{events:{seq:number;type:string;payload:Record<string,unknown>}[]}>();
const results=computed(()=>orderCompareResults(props.events));
const signed=(value:number|null)=>value===null?"未知":value>0?`+${value}`:String(value);
</script>
<template>
 <section v-for="row in results" :key="row.seq" aria-label="订单双窗对比结果">
  <h4>订单数量双窗口对比</h4><p>共同当前状态：{{row.value.status_filter}}；按创建时间，不是历史状态快照。视图 {{row.value.view_version}} · 策略 {{row.value.policy_version}}</p>
  <p>基准 [{{row.value.baseline_since}}, {{row.value.baseline_until}})；当前 [{{row.value.current_since}}, {{row.value.current_until}})</p>
  <p v-if="!row.value.complete" role="alert">对比不可用：{{row.value.reason}}；两窗均未知，不展示部分计数、不补0。</p>
  <template v-else>
   <table aria-label="订单双窗对比数据"><thead><tr><th>基准数量</th><th>当前数量</th><th>差值（当前－基准）</th><th>相对变化（精确差值/基准）</th></tr></thead><tbody><tr><td>{{row.value.baseline_count}}</td><td>{{row.value.current_count}}</td><td>{{signed(row.value.delta_count)}}</td><td>{{row.value.relative_denominator===null?'未知':`${row.value.relative_numerator} / ${row.value.relative_denominator}`}}</td></tr></tbody></table>
   <p v-if="row.value.baseline_count===0">基准数量为0，相对变化未知；不补0%、不显示无穷，真实计数和差值保留。</p>
  </template>
  <p>同一完整授权来源、返回前二次核验；不是事务快照。不返回订单明细，不作百分比舍入。</p>
 </section>
</template>
