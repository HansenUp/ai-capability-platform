<script setup lang="ts">
import {reactive, ref, watch} from "vue";
import Chat from "./Chat.vue";
import {previewOrderCount, proposeOrderCount, type OrderCountPlan} from "../order-count-query";
const form = reactive({since:"2026-10-01T00:00:00Z", until:"2026-10-02T00:00:00Z", status:"all"});
const language = ref("");
const plan = ref<OrderCountPlan | null>(null), error = ref("");
const selected = ref<string | null>(null);
function clear() { plan.value = null; error.value = ""; }
watch(form, clear, {deep:true, flush:"sync"});
watch(language, clear, {flush:"sync"});
function build(natural = false) {
  clear();
  try { plan.value = natural ? proposeOrderCount(language.value) : previewOrderCount(form); }
  catch (failure) { error.value = failure instanceof Error ? failure.message : "无法生成统计候选。"; }
}
</script>
<template>
  <p><router-link to="/manage/queries">受限查询统一入口与能力说明</router-link></p>
  <h2>订单日期、状态与数量 · 合成隔离查询</h2>
  <p>独立经营视图 synthetic_orders/v1，不复用旧orders商品字段权限。仅专用隔离经营环境可执行；普通演示不会自动启用。<a href="/manage/data-views">返回商品明细查询</a></p>
  <section aria-label="订单统计字段与口径">
    <h3>字段、口径与权限</h3>
    <p>order_id为租户内订单唯一标识；created_at为带时区创建时间；status仅pending/completed/cancelled，表示读取时当前状态。updated_at仅为源更新证据，不作为历史状态。</p>
    <p>创建时间使用[起点,终点)，最多31天；不猜测“今天”或业务时区。按订单ID去重，订单ID冲突、无效创建时间/状态或来源超过100行时数量未知，不从截断样本推算总量。</p>
    <p>只交付数量与状态分布，不交付原始ID、时间明细、金额或敏感字段。合成测试仅Alice获精确read_data；Bob、管理员、跨租户、服务身份不能获得隐式放行。真实身份、正式业务数据和模型未接入。</p>
  </section>
  <p><a href="/manage/order-groups">订单金额与受限分组查询（独立工具）</a></p>
  <form @submit.prevent="build()">
    <label>创建起点（含时区）<input v-model="form.since" /></label>
    <label>创建终点（不含，含时区）<input v-model="form.until" /></label>
    <label>当前订单状态<select v-model="form.status" aria-label="当前订单状态"><option value="all">全部</option><option value="pending">待完成</option><option value="completed">已完成</option><option value="cancelled">已取消</option></select></label>
    <button>预览订单数量查询</button>
  </form>
  <section aria-label="订单统计自然语言">
    <p>句式：统计创建时间从“2026-10-01T00:00:00Z”到“2026-10-02T00:00:00Z”的已完成订单数量</p>
    <label>订单统计描述<textarea v-model="language" maxlength="512" /></label>
    <button @click="build(true)">生成订单统计候选</button>
  </section>
  <p v-if="error" role="alert">{{error}}</p>
  <section v-if="plan" aria-label="订单统计候选">
    <h3>待确认的统计范围</h3><pre>{{JSON.stringify(plan.request, null, 2)}}</pre>
    <ol><li v-for="line in plan.explanation" :key="line">{{line}}</li></ol>
    <button @click="selected = '!tool analytics.order_count ' + JSON.stringify(plan.request)">确认范围并进入订单统计工作台</button>
  </section>
  <section v-if="selected !== null" aria-label="订单统计执行">
    <p>应用本人签名测试身份，填入草稿后登记、显式执行一次；未知结果只读恢复，终结回执需本人ACK。编辑上方候选不会覆盖已登记请求；页面重载只读恢复原账本，不自动重发。</p>
    <Chat :isolated-count-text="selected" />
  </section>
</template>
