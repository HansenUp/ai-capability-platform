<script setup lang="ts">
import {reactive,ref,watch} from "vue";
import Chat from "./Chat.vue";
import {previewOrderCompare,proposeOrderCompare,compareCapability} from "../order-compare-query";
const form=reactive({baseline_since:"2026-10-01T00:00:00Z",baseline_until:"2026-10-02T00:00:00Z",current_since:"2026-10-02T00:00:00Z",current_until:"2026-10-03T00:00:00Z",status:"all"});
const language=ref(""),error=ref(""),selected=ref<string|null>(null),plan=ref<ReturnType<typeof previewOrderCompare>|null>(null);
const clear=()=>{plan.value=null;error.value="";};watch(form,clear,{deep:true,flush:"sync"});watch(language,clear,{flush:"sync"});
function build(natural=false){clear();try{plan.value=natural?proposeOrderCompare(language.value):previewOrderCompare(form);}catch(e){error.value=e instanceof Error?e.message:"无法生成对比候选。";}}
</script>
<template>
 <p><router-link to="/manage/queries">受限查询统一入口与能力说明</router-link></p>
 <h2>双窗口订单数量对比 · 合成隔离查询</h2>
 <p>只读独立经营视图，两窗等长且不重叠；基准在前，可有间隔。两个创建窗口使用同一读取时当前状态，不是历史快照。</p>
 <p>显式时区、[起点,终点)、各最长31经过天；同一完整来源最多100行，异常两窗均未知。精确差值/基准比例，零基数未知。仅数量，不含金额、收入或退款；页面可见不代表已授权。</p>
 <form @submit.prevent="build()">
  <label>基准创建起点（含时区）<input v-model="form.baseline_since" /></label><label>基准创建终点（不含，含时区）<input v-model="form.baseline_until" /></label>
  <label>当前创建起点（含时区）<input v-model="form.current_since" /></label><label>当前创建终点（不含，含时区）<input v-model="form.current_until" /></label>
  <label>对比共同状态<select v-model="form.status" aria-label="对比共同状态"><option value="all">全部</option><option value="completed">已完成</option><option value="pending">待完成</option><option value="cancelled">已取消</option></select></label><button>预览订单双窗对比</button>
 </form>
 <section aria-label="订单对比自然语言"><p>句式：{{compareCapability.example}}</p><label>订单对比描述<textarea v-model="language" maxlength="512" /></label><button @click="build(true)">生成订单对比候选</button></section>
 <p v-if="error" role="alert">{{error}}</p>
 <section v-if="plan" aria-label="订单对比候选"><pre>{{JSON.stringify(plan.request,null,2)}}</pre><ol><li v-for="line in plan.explanation" :key="line">{{line}}</li></ol><button @click="selected='!tool analytics.order_compare '+JSON.stringify(plan.request)">确认范围并进入订单对比工作台</button></section>
 <section v-if="selected!==null" aria-label="订单对比执行"><p>本人签名身份、只读核账、填草稿后显式登记执行一次；结果未知只读恢复，终结回执需本人ACK。编辑候选不覆盖已登记请求，重载不自动重发。</p><Chat :isolated-compare-text="selected" /></section>
</template>
