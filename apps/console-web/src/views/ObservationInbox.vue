<script setup lang="ts">
import {onBeforeUnmount,ref,watch} from "vue";
import {a2aFailure} from "../a2a-guidance";
interface Item {receipt_id:number;event_id:string;agent_id:string;kind:"usage"|"status";received_at:string;trust:string;billing_eligible:boolean;forwarded:boolean;payload:{model?:string;input_tokens?:number;output_tokens?:number;cost?:number|null;currency?:string|null;instances?:number;healthy?:boolean}}
const draft=ref(""),applied=ref(false),busy=ref(false),message=ref("");
const rows=ref<Item[]>([]),next=ref<number|null>(null);
let token="",epoch=0,controller:AbortController|null=null;
function clear(){epoch++;controller?.abort();controller=null;rows.value=[];next.value=null;busy.value=false;message.value="";}
watch(draft,()=>{clear();token="";applied.value=false;},{flush:"sync"});
function apply(){const value=draft.value.trim();draft.value="";clear();token=value;applied.value=!!value;}
async function read(after=0){
 if(busy.value||!applied.value)return;
 clear();const current=epoch;const active=new AbortController();controller=active;busy.value=true;
 const timeout=setTimeout(()=>active.abort(),15000);
 try{
  const response=await fetch(`/v1/isolated-observe/events?limit=20&after=${after}`,{headers:{Authorization:`Bearer ${token}`},signal:active.signal,cache:"no-store",redirect:"error",credentials:"omit"});
  if(!response.ok)throw new Error(a2aFailure(response.status));
  const value=await response.json();if(current!==epoch)return;
  if(value.trust!=="unverified"||value.billing_eligible!==false||!Array.isArray(value.items)||
   !value.items.every((r:Item)=>r.trust==="unverified"&&r.billing_eligible===false&&r.forwarded===false)||
   !(value.next_after===null||(Number.isSafeInteger(value.next_after)&&value.next_after>after)))throw new Error("来源标记或分页证据不匹配");
  rows.value=value.items;next.value=value.next_after;message.value=rows.value.length?"只读查询完成；这些自报数据未验证，不进入可信费用统计。":"本页无记录；不代表模型消耗为零。";
 }catch(error){if(current===epoch){rows.value=[];next.value=null;message.value=String(error);}}
 finally{clearTimeout(timeout);if(current===epoch){busy.value=false;controller=null;}}
}
onBeforeUnmount(()=>{clear();token="";});
</script>
<template><section>
<h1>第三方观测隔离收件箱</h1>
<p>仅同租户验签管理员只读。服务身份经固定测试绑定接收数据；内容仍是未验证自报，不是账单、可信反馈或Run执行证据。未向Langfuse转发。</p>
<label>管理员测试令牌 <input v-model="draft" type="password" autocomplete="off" /></label>
<button @click="apply">应用身份并清空观测</button>
<button :disabled="!applied||busy" @click="read()">查询观测首页</button>
<button :disabled="!applied||busy||next===null" @click="read(next!)">下一页观测</button>
<p role="status">{{message}}</p><p>每页20条，实时列表不是历史快照。费用缺失显示未知；零只表示上报值为零。</p>
<article v-for="row in rows" :key="row.receipt_id" aria-label="未验证观测记录">
<h2>{{row.agent_id}} · {{row.kind}}</h2><p>未验证 / 不参与可信计费 / 未转发</p>
<p>事件：{{row.event_id}}；接收时间：{{row.received_at}}</p>
<template v-if="row.kind==='usage'"><p>模型：{{row.payload.model}}；输入Token：{{row.payload.input_tokens}}；输出Token：{{row.payload.output_tokens}}</p><p>自报费用：{{row.payload.cost===null?'未知':row.payload.cost}} {{row.payload.currency??''}}</p></template>
<p v-else>自报实例：{{row.payload.instances}}；自报健康：{{row.payload.healthy?'是':'否'}}</p>
</article>
</section></template>
<style scoped>label{display:block;margin:1rem 0}input{max-width:100%}article{border:1px solid #d1d5db;padding:1rem;margin:1rem 0;overflow-wrap:anywhere}</style>
