<script setup lang="ts">
import {reactive,ref,watch} from "vue";
import Chat from "./Chat.vue";
import {previewOrderGroups,proposeOrderGroups} from "../order-group-query";
const form=reactive({since:"2026-10-01T00:00:00Z",until:"2026-10-02T00:00:00Z",status:"completed",metric:"completed_amount",group_by:"currency"});
const language=ref(""),error=ref(""),selected=ref<string|null>(null);
const plan=ref<ReturnType<typeof previewOrderGroups>|null>(null);
const clear=()=>{plan.value=null;error.value="";};watch(form,clear,{deep:true,flush:"sync"});watch(language,clear,{flush:"sync"});
function build(natural=false){clear();try{plan.value=natural?proposeOrderGroups(language.value):previewOrderGroups(form);}catch(e){error.value=e instanceof Error?e.message:"无法生成分组候选。";}}
</script>
<template>
  <h2>订单金额与分组 · 合成隔离查询</h2>
  <p>独立经营视图synthetic_orders/v1，仅专用合成环境可执行。<a href="/manage/order-counts">返回日期状态数量查询</a></p>
  <section aria-label="金额与分组口径"><h3>字段、业务口径与权限</h3>
    <p>金额使用amount_minor及currency；CNY/USD均100最小单位=1主单位。只统计当前已完成订单，按币种分别列示；不是实收、收入、利润或GMV，不含退款/税费/换汇。</p>
    <p>数量可按状态、币种或状态＋币种分组。只列实际存在的组；无效币种阻止依赖币种的分组，不开放客户、员工、日期分桶、排行或任意字段。</p>
    <p>按创建时间左闭右开、最长31天，按订单ID去重；来源截断/冲突或无效创建时间/状态时未知。缺失金额使正式总额未知，保留已知小计与有效金额条数/完成订单数；有效零金额保留0。</p>
    <p>仅原经营profile精确授权Alice；Bob/管理员无隐式许可。只返回聚合，不返回订单ID或明细，不扩大F4与数量工具权限，不调用真实模型。</p>
  </section>
  <form @submit.prevent="build()">
    <label>分组创建起点（含时区）<input v-model="form.since" /></label>
    <label>分组创建终点（不含，含时区）<input v-model="form.until" /></label>
    <label>分组当前状态<select v-model="form.status" aria-label="分组当前状态"><option value="all">全部</option><option value="completed">已完成</option><option value="pending">待完成</option><option value="cancelled">已取消</option></select></label>
    <label>统计指标<select v-model="form.metric" aria-label="统计指标"><option value="completed_amount">已完成订单金额</option><option value="order_count">订单数量</option></select></label>
    <label>分组维度<select v-model="form.group_by" aria-label="分组维度"><option value="currency">币种</option><option value="status">状态</option><option value="status_currency">状态＋币种</option></select></label>
    <button>预览订单分组查询</button>
  </form>
  <section aria-label="订单分组自然语言"><p>句式：统计创建时间从“2026-10-01T00:00:00Z”到“2026-10-02T00:00:00Z”的已完成订单金额，按币种分组</p>
    <label>订单分组描述<textarea v-model="language" maxlength="512" /></label><button @click="build(true)">生成订单分组候选</button>
  </section>
  <p v-if="error" role="alert">{{error}}</p>
  <section v-if="plan" aria-label="订单分组候选"><pre>{{JSON.stringify(plan.request,null,2)}}</pre><ol><li v-for="line in plan.explanation" :key="line">{{line}}</li></ol><button @click="selected='!tool analytics.order_groups '+JSON.stringify(plan.request)">确认范围并进入订单分组工作台</button></section>
  <section v-if="selected!==null" aria-label="订单分组执行"><p>应用本人签名身份并显式填入/登记/执行一次；未知只读恢复，终结回执本人ACK。编辑候选不覆盖已登记请求，重载不自动重发。</p><Chat :isolated-groups-text="selected" /></section>
</template>
