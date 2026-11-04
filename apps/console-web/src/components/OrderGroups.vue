<script setup lang="ts">
import {computed} from "vue";
import {orderGroupResults} from "../order-group-result";
const props=defineProps<{events:{seq:number;type:string;payload:Record<string,unknown>}[]}>();
const rows=computed(()=>orderGroupResults(props.events));
const status=(s:string|undefined)=>s?({pending:"待完成",completed:"已完成",cancelled:"已取消"}[s]??s):"—";
</script>
<template>
  <section v-for="row in rows" :key="row.seq" aria-label="订单分组结果">
    <h4>{{row.value.metric==='completed_average'?'已完成订单平均金额（分币种）':row.value.metric==='completed_amount'?'已完成订单金额（分币种）':'订单数量分组'}}</h4>
    <p>创建窗口 [{{row.value.since}}, {{row.value.until}})；读取时当前状态，非历史快照。视图 {{row.value.view_version}} · 策略 {{row.value.policy_version}}</p>
    <p v-if="!row.value.complete" role="alert">统计不可用：{{row.value.reason}}；不补0。</p>
    <template v-else>
      <p v-if="!row.value.groups.length">所选范围无分组；没有可用金额组不等于0金额。</p>
      <table v-else aria-label="订单分组数据"><thead><tr><th>状态</th><th>币种</th><th>订单数</th><template v-if="row.value.metric!=='order_count'"><th>正式总额（最小单位）</th><th>已知小计（最小单位）</th><th>金额覆盖</th><th v-if="row.value.metric==='completed_average'">平均金额（精确最小单位/单）</th></template></tr></thead>
        <tbody><tr v-for="g in row.value.groups" :key="JSON.stringify(g.key)"><td>{{status(g.key.status)}}</td><td>{{g.key.currency??'—'}}</td><td>{{g.order_count}}</td><template v-if="row.value.metric!=='order_count'"><td>{{g.total_minor??'未知'}}</td><td>{{g.known_minor??'未知'}}</td><td>{{g.valid_amount_count}} / {{g.order_count}}</td><td v-if="row.value.metric==='completed_average'">{{g.average_numerator_minor===null?'未知':`${g.average_numerator_minor} / ${g.average_denominator}`}}</td></template></tr></tbody>
      </table>
      <p v-if="row.value.metric!=='order_count'&&row.value.groups.some(g=>!g.amount_complete)" role="alert">存在缺失或无效金额；正式总额未知，已知小计不是完整总额。</p>
    </template>
    <p v-if="row.value.metric==='completed_average'">均值以完整金额总和/订单数表示精确分数；金额不完整时未知，不做子样本平均或舍入。</p>
    <p>CNY分/USD美分，100最小单位=1主单位；不换汇、不跨币种合计，不代表实收、收入或利润。仅同Run匹配工具回执，不返回明细。</p>
  </section>
</template>
