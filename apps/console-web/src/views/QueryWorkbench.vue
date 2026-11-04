<script setup lang="ts">
import {ref,watch} from "vue";
import {assessQuery,queryCapabilities,type QueryAssessment} from "../query-assessment";
import {proposeOrderCompare,compareCapability} from "../order-compare-query";
const capabilities=[...queryCapabilities,compareCapability];
function assess(text:string,version:string):QueryAssessment{
  try{return {status:"supported",reason:"当前合成契约能够生成候选；尚未授权或执行。",missing:[],target:compareCapability.name,route:compareCapability.route,request:proposeOrderCompare(text).request};}
  catch{return assessQuery(text,version);}
}
const text=ref(""),version=ref("v1"),assessment=ref<QueryAssessment|null>(null);
watch([text,version],()=>{assessment.value=null;},{flush:"sync"});
const labels={supported:"已支持（待授权候选）",needs_clarification:"需补充信息",not_supported:"待开发或尚未支持",scope_rejected:"权限范围拒绝"};
</script>
<template>
  <h2>受限查询统一入口</h2>
  <p>先核对能力和参数，再进入对应查询页。这里只生成本机候选，不调用模型或业务接口，不创建Run，不传递身份或授权。</p>
  <table aria-label="受限查询能力表"><thead><tr><th>能力</th><th>当前范围</th><th>入口</th></tr></thead><tbody><tr v-for="c in capabilities" :key="c.id"><td>{{c.name}}</td><td>{{c.scope}}</td><td><router-link :to="c.route">{{c.name}}入口</router-link> <button @click="text=c.example">填入{{c.name}}示例</button></td></tr></tbody></table>
  <p>商品字段明细与独立经营统计使用不同视图和工具。只有专用合成环境可执行经营统计；页面可见不代表有read_data许可。</p>
  <form @submit.prevent="assessment=assess(text,version)">
    <label>统一查询描述<textarea v-model="text" maxlength="512" /></label>
    <label>商品视图版本<input v-model="version" maxlength="64" /></label>
    <button>评估问法并生成候选</button>
  </form>
  <section v-if="assessment" aria-label="统一问法评估">
    <h3>{{labels[assessment.status]}}</h3><p>{{assessment.reason}}</p>
    <ul v-if="assessment.missing.length"><li v-for="item in assessment.missing" :key="item">{{item}}</li></ul>
    <template v-if="assessment.status==='supported'"><p>目标：{{assessment.target}}</p><pre aria-label="统一请求候选">{{JSON.stringify(assessment.request,null,2)}}</pre><router-link :to="assessment.route!">打开对应查询页核对并确认</router-link><p>入口跳转不携带请求或令牌。请在对应页显式填写和确认，再应用本人身份、登记、执行一次、核对结果并本人ACK。</p></template>
  </section>
  <p>“权限范围拒绝”用于拒绝描述中的越界要求，不能替代服务端身份验签、当前清单和精确read_data核验。未支持、缺参和拒绝均不生成执行请求。</p>
</template>
