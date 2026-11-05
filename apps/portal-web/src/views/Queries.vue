<script setup lang="ts">
import {inject,reactive,ref,watch} from "vue";
import Workbench from "../../../console-web/src/views/Chat.vue";
import {getToken} from "../auth";
import {portalTokenKey} from "../runtime-session";
import {portalCapabilities,proposePortalQuery,previewPortalQuery,type PortalCandidate} from "../query-candidate";
const tokenProvider=inject(portalTokenKey,getToken);
const text=ref(""),version=ref("v1"),error=ref("");
const form=reactive({kind:"count",since:"2026-10-01T00:00:00Z",until:"2026-10-02T00:00:00Z",status:"all",metric:"order_count",group_by:"status",bucket_timezone:"UTC"});
const preview=ref<PortalCandidate|null>(null),selected=ref<PortalCandidate|null>(null);
function clear(){preview.value=null;error.value="";}
watch([text,version],clear,{flush:"sync"});watch(form,clear,{deep:true,flush:"sync"});
function build(language=false){clear();try{preview.value=language?proposePortalQuery(text.value,version.value):previewPortalQuery(form);}catch(e){error.value=e instanceof Error?e.message:"无法生成候选";}}
function confirm(){if(preview.value)selected.value=JSON.parse(JSON.stringify(preview.value));}
</script>
<template>
  <router-link to="/">返回可用 Agent</router-link> · <router-link to="/tasks">我的任务</router-link>
  <h2>Portal 受限查询</h2>
  <p>本机合成查询：先预览范围，再应用本人门户身份、登记、显式执行一次和本人ACK。页面可见不代表获得 read_data 权限。正式业务数据与模型未接入。</p>
  <section aria-label="查询口径与权限"><h3>查询口径与权限</h3>
    <p>创建窗口左闭右开、显式时区、最多31经过天；状态为读取时当前状态，非历史快照。按订单ID去重，全源冲突、无效事实或超过100行时结果未知，不用样本推算总量。</p>
    <p>金额仅已完成订单、分币种列示，不等于实收或收入；金额不完整时总额与均值未知。占比分母为同窗口或当日全部有效订单，空分母未知，不补0%。按日只接受UTC或UTC+08:00，不跨日合并。</p>
    <p>双窗口对比要求等长且不重叠、同一完整授权来源；基准为0时比例未知。所有工具由服务端重新核验精确权限；管理员、其他用户或其他租户不隐式放行。</p>
  </section>
  <ul><li v-for="cap in portalCapabilities" :key="cap.id">{{cap.name}}：{{cap.scope}} <button type="button" @click="text=cap.example">填入{{cap.name}}示例</button></li></ul>
  <form @submit.prevent="build(true)"><label>查询描述<textarea v-model="text" maxlength="512" /></label><label>商品视图版本<input v-model="version" maxlength="64" /></label><button>生成门户查询候选</button></form>
  <form @submit.prevent="build()">
    <label>查询类型<select v-model="form.kind"><option value="count">订单数量</option><option value="groups">受限分组</option></select></label>
    <label>创建起点（含时区）<input v-model="form.since" /></label><label>创建终点（不含，含时区）<input v-model="form.until" /></label>
    <label>当前状态<select v-model="form.status"><option value="all">全部</option><option value="pending">待完成</option><option value="completed">已完成</option><option value="cancelled">已取消</option></select></label>
    <template v-if="form.kind==='groups'"><label>指标<select v-model="form.metric"><option value="order_count">数量</option><option value="completed_amount">已完成金额</option><option value="completed_average">已完成平均金额</option><option value="status_share">状态占比</option></select></label>
    <label>分组<select v-model="form.group_by"><option value="status">状态</option><option value="currency">币种</option><option value="status_currency">状态和币种</option><option value="day">日</option><option value="day_currency">日和币种</option><option value="day_status">日和状态</option></select></label>
    <label v-if="form.group_by.startsWith('day')">分桶时区<select v-model="form.bucket_timezone"><option>UTC</option><option>UTC+08:00</option></select></label></template>
    <button>预览门户查询</button>
  </form>
  <p v-if="error" role="alert">{{error}}</p>
  <section v-if="preview" aria-label="门户查询候选"><h3>待确认的查询范围</h3><p>固定 Agent：{{preview.agent}}</p><pre>{{JSON.stringify(preview.request,null,2)}}</pre><ul><li v-for="line in preview.explanation" :key="line">{{line}}</li></ul><button type="button" @click="confirm">确认范围并打开门户查询工作台</button></section>
  <section v-if="selected" aria-label="门户查询执行">
    <p>本次已确认的原始候选（上方编辑不会覆盖）：</p><pre>{{selected.text}}</pre>
    <p>重载后请重新预览并应用身份，从服务端账本只读恢复；未知结果不自动重发，不绕过未决回执。身份切换清空旧结果，终结后由本人ACK。</p>
    <Workbench :key="selected.text" :portal-agent="selected.agent" :portal-token-provider="tokenProvider"
      :isolated-query-text="selected.agent==='f4-data-query'?selected.text:undefined"
      :isolated-count-text="selected.agent==='f10-order-count'?selected.text:undefined"
      :isolated-groups-text="selected.agent==='f10-order-groups'?selected.text:undefined"
      :isolated-compare-text="selected.agent==='f10-order-compare'?selected.text:undefined" />
  </section>
</template>
