<script setup lang="ts">
import SyntheticEvaluationHistory from "./SyntheticEvaluationHistory.vue";
import {onBeforeUnmount, ref} from "vue";
const props=defineProps<{token:string;agent:string;datasetId:string}>();
type Child={case_id:string;run_id:string;evidence:null|{state:string;judgement:string;reasons:string[]}};
type Attempt={attempt_id:string;scope_id:string;fingerprint:string;revision:number;status:string;acknowledged:boolean;
 binding:{agent_id:string;agent_version:string;dataset_version:string};children:Child[];
 receipt:null|{result:{selected_count?:number;verified_count?:number;passed_count?:number;pass_rate?:number|null}}};
const version=ref("1"), active=ref<Attempt|null>(null), busy=ref(false), ready=ref(false), blocked=ref(false), confirmed=ref(false), message=ref("");
let generation=0, controller:AbortController|null=null, scope="";
onBeforeUnmount(()=>{generation++;controller?.abort();active.value=null;scope="";});
async function call(path:string,body?:unknown){
 const response=await fetch(`/v1/${path}`,{method:body===undefined?"GET":"POST",headers:{Authorization:`Bearer ${props.token}`,...(body===undefined?{}:{"Content-Type":"application/json"})},body:body===undefined?undefined:JSON.stringify(body),cache:"no-store",credentials:"omit",redirect:"error",signal:controller?.signal});
 if(!response.ok)throw new Error(`Runtime HTTP ${response.status}`);return response.json();
}
async function refresh(){
 if(busy.value||!props.token)return;
 const current=++generation;controller?.abort();controller=new AbortController();busy.value=true;ready.value=false;active.value=null;confirmed.value=false;
 try{
  const identity=await call("workbench/identity");if(current!==generation)return;
  if(identity.auth_method!=="user"||typeof identity.scope_id!=="string")throw new Error("验签用户身份不匹配");scope=identity.scope_id;
  const value=await call("isolated-evaluations/ledger");if(current!==generation)return;
  if(typeof value.blocked_elsewhere!=="boolean"||value.active&&(value.active.scope_id!==scope||!["PREPARED","EXECUTING","COMMITTED","REJECTED_NO_EFFECT"].includes(value.active.status)))throw new Error("评测账本范围不匹配");
  active.value=value.active;blocked.value=value.blocked_elsewhere;ready.value=true;message.value="已只读核对；不会补跑、重发或自动解锁。";
 }catch(error){if(current===generation){active.value=null;message.value=String(error)+"；无法核对，不代表没有执行。";}}
 finally{if(current===generation)busy.value=false;}
}
async function mutate(path:string,body:unknown){
 const current=generation;busy.value=true;ready.value=false;confirmed.value=false;
 try{await call(path,body);if(current===generation)message.value="请求已返回，请只读恢复核对；不自动执行下一步。";}
 catch(error){if(current===generation)message.value=String(error)+"；结果可能未知，只读恢复原登记，禁止自动重发。";}
 finally{if(current===generation)busy.value=false;}
}
async function prepare(){
 if(!ready.value||busy.value||active.value||blocked.value||!props.datasetId||!version.value.trim())return;
 await mutate("isolated-evaluations/prepare",{attempt_id:crypto.randomUUID(),dataset_id:props.datasetId,agent_id:props.agent,agent_version:version.value});
}
async function action(name:"execute"|"acknowledge"){
 const row=active.value;if(!ready.value||busy.value||!row)return;
 if(name==="execute"&&row.status!=="PREPARED")return;
 if(name==="acknowledge"&&(!confirmed.value||!["COMMITTED","REJECTED_NO_EFFECT"].includes(row.status)))return;
 await mutate(`isolated-evaluations/${row.attempt_id}/${name}`,{fingerprint:row.fingerprint,revision:row.revision});
}
</script>
<template>
 <section class="panel" aria-label="隔离合成评测执行">
  <h3>合成评测 · 仅本机诊断</h3>
  <p>复用上方签名身份。选择已冻结数据集及明确的Runtime版本；管理分配与精确执行授权必须同时满足。不授予发布资格，不写入Langfuse。</p>
  <p>隔离演示提供工单测试Agent f11-dataset-demo / 1。缺少已发布快照时拒绝，不会自动发布或回退版本。</p>
  <button :disabled="busy" @click="refresh">只读恢复评测账本</button>
  <p role="status">{{message}}</p>
  <p v-if="blocked">同主体其他未决写尚未收口，请返回原模块恢复并ACK。</p>
  <fieldset :disabled="busy||!ready||blocked||!!active"><legend>新评测登记</legend>
   <p>冻结数据集：{{datasetId||'请先在上方选择冻结版本'}}</p>
   <label>评测 Runtime 明确版本 <input v-model="version" maxlength="64" /></label>
   <button :disabled="!datasetId" @click="prepare">登记本次合成评测</button>
  </fieldset>
  <section v-if="active" aria-label="合成评测回执">
   <p>{{active.status}} · {{active.binding.agent_id}} / {{active.binding.agent_version}} · 数据集{{active.binding.dataset_version}}</p>
   <p>attempt：{{active.attempt_id}} · 回执修订{{active.revision}}</p>
   <p v-if="active.status==='EXECUTING'">执行未决：可能仍在执行或已中断。仅可只读核对，不补跑、不撤锁；需要另行处理。</p>
   <button v-if="active.status==='PREPARED'" :disabled="busy||!ready" @click="action('execute')">显式执行本次合成评测一次</button>
   <dl v-if="active.receipt?.result.selected_count!==undefined">
    <dt>选中 / 已验证 / 通过</dt><dd>{{active.receipt.result.selected_count}} / {{active.receipt.result.verified_count}} / {{active.receipt.result.passed_count}}</dd>
    <dt>通过率</dt><dd>{{active.receipt.result.pass_rate==null?'未知（证据不完整）':`${active.receipt.result.pass_rate*100}%`}}</dd>
   </dl>
   <ul aria-label="合成评测用例结果"><li v-for="child in active.children" :key="child.case_id">{{child.case_id}} · {{child.run_id}} · {{child.evidence?.judgement||'未决'}} · {{child.evidence?.reasons.join('、')}}</li></ul>
   <template v-if="['COMMITTED','REJECTED_NO_EFFECT'].includes(active.status)">
    <label><input v-model="confirmed" type="checkbox" :disabled="busy||!ready" />核对评测终结回执后本人确认</label>
    <button :disabled="busy||!ready||!confirmed" @click="action('acknowledge')">本人ACK评测回执</button>
   </template>
  </section>
  <SyntheticEvaluationHistory :token="token" :agent="agent" />
 </section>
</template>
