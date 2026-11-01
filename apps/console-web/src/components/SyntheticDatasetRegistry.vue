<script setup lang="ts">
import {onBeforeUnmount, ref} from "vue";
import SyntheticEvaluation from "./SyntheticEvaluation.vue";
const evaluationToken=ref(""), evaluationKey=ref(0);
const props = defineProps<{proposal: {scenario_id:string;catalog_revision:string;case_ids:string[]} | null}>();
type Entry={id:string;agent_id:string;version:string;state:string;revision:number;maintainer:string;proposal:{scenario_id:string;case_ids:string[]}};
type Attempt={scope_id:string;attempt_id:string;fingerprint:string;revision:number;status:string;acknowledged:boolean;command:{action:string;agent_id:string;version:string};evidence:unknown};
const actor=ref("");
const demoRole=ref("dataset_admin");
let demoTokens: Record<string,string> = {}, demoExpires = 0;
const tokenInput=ref(""), agent=ref(""), version=ref("1"), message=ref(""), subject=ref("");
const entries=ref<Entry[]>([]), active=ref<Attempt|null>(null), selected=ref<Entry|null>(null);
const authorized=ref(false);
const busy=ref(false), ready=ref(false), elsewhere=ref(false), ackPreview=ref(false);
let token="", scope="", generation=0, controller:AbortController|null=null;
function clear() { evaluationToken.value="";evaluationKey.value++;generation++;controller?.abort();token="";scope="";actor.value="";subject.value="";entries.value=[];active.value=null;selected.value=null;ready.value=false;authorized.value=false;busy.value=false;elsewhere.value=false;ackPreview.value=false; }
onBeforeUnmount(() => { clear(); demoTokens={}; });
async function call(path:string,body?:unknown) {
 const r=await fetch(`/v1/${path}`,{method:body===undefined?"GET":"POST",headers:{Authorization:`Bearer ${token}`,...(body===undefined?{}:{"Content-Type":"application/json"})},body:body===undefined?undefined:JSON.stringify(body),signal:controller?.signal,cache:"no-store",credentials:"omit",redirect:"error"});
 if(!r.ok)throw new Error(`Runtime HTTP ${r.status}`);return r.json();
}
async function apply() {const next=tokenInput.value;clear();tokenInput.value="";token=next;evaluationToken.value=next;await refresh();}
async function refresh() {
 if(!token||busy.value)return;
 const current=generation;busy.value=true;ready.value=false;authorized.value=false;entries.value=[];selected.value=null;active.value=null;ackPreview.value=false;controller=new AbortController();
 try {
  const identity=await call("workbench/identity");if(current!==generation)return;
  if(identity.auth_method!=="user"||typeof identity.scope_id!=="string")throw new Error("验签身份无效");
  scope=identity.scope_id;actor.value=identity.subject;subject.value=`${identity.tenant_id} / ${identity.subject}`;
  const ledger=await call("isolated-datasets/ledger");if(current!==generation)return;
  if(ledger.scope_id!==scope||typeof ledger.blocked_elsewhere!=="boolean"||ledger.active&&(ledger.active.scope_id!==scope||!["PREPARED","COMMITTED","REJECTED_NO_EFFECT"].includes(ledger.active.status)))throw new Error("账本范围不匹配");
  active.value=ledger.active;elsewhere.value=ledger.blocked_elsewhere;ready.value=true;
  message.value="已核对本人账本；终结回执需独立ACK。";
  if(agent.value){
   const listing=await call(`isolated-datasets?agent_id=${encodeURIComponent(agent.value)}`);if(current!==generation)return;
   if(listing.identity.scope_id!==scope||!Array.isArray(listing.items))throw new Error("数据集范围不匹配");
   entries.value=listing.items;authorized.value=true;
  }
 }catch(error){if(current===generation){entries.value=[];message.value=String(error)+"；未查到不代表没有写入，不自动重发。";}}
 finally {if(current===generation)busy.value=false;}
}
async function prepare(action:string) {
 if(!ready.value||!authorized.value||busy.value||active.value||elsewhere.value||!agent.value)return;
 if(action!=="freeze"&&!props.proposal)return;
 const row=selected.value;if(action!=="draft"&&!row)return;
 const command={attempt_id:crypto.randomUUID(),action,agent_id:agent.value,version:action==="draft"?version.value:row!.version,registration_id:action==="draft"?null:row!.id,expected_revision:action==="draft"?0:row!.revision,proposal:action==="freeze"?null:{schema_version:1,...props.proposal}};
 await mutate("isolated-datasets/prepare",command,"已登记，尚未执行；请只读恢复后核对。");
}
async function mutate(path:string,body:unknown,success:string) {
 const current=generation;busy.value=true;ready.value=false;ackPreview.value=false;
 try {await call(path,body);if(current===generation)message.value=success;}
 catch(error){if(current===generation)message.value=String(error)+"；写结果未知，必须只读恢复，不自动重发。";}
 finally{if(current===generation)busy.value=false;}
}
async function execute() {
 const row=active.value;if(!ready.value||busy.value||!row||row.status!=="PREPARED")return;
 await mutate(`isolated-datasets/${row.attempt_id}/execute`,{fingerprint:row.fingerprint,revision:row.revision},"执行已返回；请只读核对终结回执，再本人ACK。");
}
async function ack() {
 const row=active.value;if(!ready.value||busy.value||!ackPreview.value||!row||!["COMMITTED","REJECTED_NO_EFFECT"].includes(row.status))return;
 await mutate(`isolated-datasets/${row.attempt_id}/acknowledge`,{fingerprint:row.fingerprint,revision:row.revision},"本人ACK已返回；请只读核对。ACK不发布或运行评测。");
}
async function loadDemo() {
 const current=generation;
 demoTokens={};demoExpires=0;
 try {
  if(location.hostname!=="127.0.0.1")throw new Error();
  const response=await fetch("/__demo/info",{credentials:"omit",cache:"no-store",redirect:"error"});
  if(!response.ok)throw new Error();
  const value=await response.json(),expires=Date.parse(value.expires_at);
  if(current!==generation)return;
  if(!Number.isFinite(expires)||expires<=Date.now()||expires-Date.now()>1800000||["dataset_admin","dataset_unassigned"].some(key=>typeof value.dataset_tokens?.[key]!=="string"||!value.dataset_tokens[key]))throw new Error();
  demoTokens=value.dataset_tokens;demoExpires=expires;message.value="数据集测试身份已载入内存，请显式应用；服务端仍核验权限。";
 }catch{if(current===generation)message.value="数据集测试身份不可用或已到期；不使用开发身份头替代。";}
}
async function applyDemo() {
 if(Date.now()>=demoExpires||!demoTokens[demoRole.value]){clear();message.value="请重新加载有效的数据集测试身份。";return;}
 tokenInput.value=demoTokens[demoRole.value];await apply();
}
function changeAgent(){clear();message.value="Agent已变化，请重新应用签名身份并核对权限。";}
</script>
<template>
 <section aria-label="隔离合成数据集管理" class="panel">
  <h3>合成数据集 · 保存与冻结</h3>
  <p>仅临时隔离环境：选择上方已校验清单，登记→只读恢复→显式执行→核对→本人ACK。冻结不是评测通过或发布授权。生产接口未启用。</p>
 <details><summary>数据集体验步骤与异常恢复</summary><ol><li>上方选择固定合成用例并校验；Agent填写f11-dataset-demo。</li><li>加载并应用“已分配管理员”，填写唯一新版本，登记保存。</li><li>只读恢复后显式执行，再只读恢复、核对终结回执并本人ACK。</li><li>选择已保存版本查看明细；原创建者可登记冻结，重复核对与ACK流程。</li></ol><p>“未分配管理员”应被拒绝；冻结不运行评测。响应丢失或重载后，用原身份只读恢复，不重新登记。到期需重启隔离演示，新库不会恢复旧临时数据。</p></details>
 <button @click="loadDemo">加载数据集测试身份</button>
 <label>数据集测试角色 <select v-model="demoRole"><option value="dataset_admin">已分配管理员</option><option value="dataset_unassigned">未分配管理员</option></select></label>
 <button @click="applyDemo">应用数据集测试角色</button>
  <label>数据集 Agent <input v-model="agent" @input="changeAgent" maxlength="64" /></label>
  <label>数据集签名测试令牌 <input v-model="tokenInput" type="password" autocomplete="off" /></label>
  <button @click="apply">应用数据集身份并清空</button>
  <button :disabled="busy" @click="refresh">只读恢复数据集账本</button>
  <p>当前验签身份：{{subject||'尚未核对'}}（不使用上方开发身份头授权）</p>
  <p role="status">{{message}}</p>
  <p v-if="elsewhere">同主体存在其他模块未决写，请返回原模块恢复与收口。</p>
  <fieldset :disabled="!ready||!authorized||busy||!!active||elsewhere">
   <legend>登记数据集动作</legend>
   <label>新数据集版本 <input v-model="version" maxlength="64" /></label>
   <button :disabled="!proposal" @click="prepare('draft')">登记保存合成草稿</button>
   <button :disabled="!proposal||!selected||selected.state!=='draft'||selected.maintainer!==actor" @click="prepare('edit')">登记替换所选草稿</button>
   <button :disabled="!selected||selected.state!=='draft'||selected.maintainer!==actor" @click="prepare('freeze')">登记冻结所选版本</button>
  </fieldset>
  <ul aria-label="合成数据集版本"><li v-for="row in entries" :key="row.id"><button :disabled="busy||!!active" @click="selected=row">选择 {{row.version}}</button> · {{row.state}} · 修订{{row.revision}}</li></ul>
  <section v-if="selected" aria-label="合成数据集只读明细"><p>{{selected.agent_id}} / {{selected.version}} / {{selected.state}} · {{selected.maintainer}}</p><p>{{selected.proposal.scenario_id}}：{{selected.proposal.case_ids.join('、')}}</p></section>
  <section v-if="active" aria-label="数据集写回执">
   <p>{{active.status}} · {{active.command.action}} · {{active.command.agent_id}} / {{active.command.version}}</p>
   <p>attempt：{{active.attempt_id}} · 回执修订{{active.revision}}</p>
   <button v-if="active.status==='PREPARED'" :disabled="!ready||busy" @click="execute">显式执行本次数据集登记</button>
   <template v-else>
    <label><input v-model="ackPreview" type="checkbox" :disabled="!ready||busy" />核对数据集终结回执后本人确认</label>
    <button :disabled="!ready||busy||!ackPreview" @click="ack">本人ACK数据集回执</button>
   </template>
  </section>
  <SyntheticEvaluation v-if="evaluationToken" :key="evaluationKey" :token="evaluationToken" :agent="agent" :dataset-id="selected?.state==='frozen'?selected.id:''" />
 </section>
</template>
