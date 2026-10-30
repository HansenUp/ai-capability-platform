<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { a2aFailure } from "../a2a-guidance";
const route=useRoute();
const props=defineProps<{mcp?:boolean}>();
const base="/v1/isolated-a2a", extension="urn:ai-platform:isolated:workbench:1";
interface Row {scope_id:string;run_id:string;attempt_id:string;action:string;status:string;revision:number;fingerprint:string;acknowledged:boolean;rejection?:string|null}
interface Evidence {profile:string;receipt:Row;messageId:string;contextId:string;taskId:string|null}
const token=ref(""),applied=ref(false),busy=ref(false),message=ref("");
const agents=ref<{agent_id:string;version:string}[]>([]),agent=ref(""),input=ref("");
const saved=ref<Evidence|null>(null),checked=ref(false),blocked=ref(true),preview=ref(false),restoreId=ref("");
const withdrawalPreview=ref(false),catalogReady=ref(false);
let bearer="",scope="",epoch=0,controller:AbortController|null=null;
const original=ref<{message:{messageId:string;role:string;parts:{text:string}[]};configuration:{returnImmediately:true}}|null>(null);
const attempted=ref(false); let attempt="";
function reset(){epoch++;controller?.abort();controller=null;busy.value=false;saved.value=null;checked.value=false;blocked.value=true;preview.value=false;withdrawalPreview.value=false;
 catalogReady.value=false;agents.value=[];agent.value="";input.value="";original.value=null;attempt="";attempted.value=false;scope="";restoreId.value="";message.value="";}
watch(token,value=>{if(value){reset();bearer="";applied.value=false;}},{flush:"sync"});
function apply(){const value=token.value.trim();reset();bearer=value;token.value="";applied.value=!!value; const hint=route.query.attempt; if(typeof hint==="string"&&/^[a-f0-9-]{36}$/.test(hint))restoreId.value=hint;}
async function request(path:string,generation:number,body?:unknown){
 if(props.mcp&&(path.startsWith(base)||path==="/v1/isolated-discovery/agents")){
  const initialize=path.endsWith("/.well-known/agent-card.json");
  const data=body as Record<string,unknown>|undefined;
  let name="",args:unknown=data??{};
  if(path==="/v1/isolated-discovery/agents")name="agents_discover";
  else if(path.endsWith("/prepare"))name="agent_prepare";
  else if(path.endsWith("/message:send"))name="agent_execute";
  else if(path.endsWith("/acknowledge"))name="attempt_ack";
  else if(path.endsWith("/withdraw"))name="attempt_withdraw";
  else if(path.includes("/attempts/")){name="attempt_recover";args={attempt_id:decodeURIComponent(path.split("/").at(-1)!)};}
  if(!initialize&&!name)throw new Error("不支持的MCP操作");
  const response=await fetch("/v1/isolated-mcp",{method:"POST",headers:{Authorization:`Bearer ${bearer}`,"Content-Type":"application/json",
   Accept:"application/json, text/event-stream","MCP-Protocol-Version":"2025-06-18","X-Platform-MCP-Profile":"isolated-agent-receipts-v1"},
   body:JSON.stringify({jsonrpc:"2.0",id:crypto.randomUUID(),method:initialize?"initialize":"tools/call",
    params:initialize?{protocolVersion:"2025-06-18",capabilities:{},clientInfo:{name:"isolated-console",version:"1"}}:{name,arguments:args}}),
   signal:controller?.signal,cache:"no-store",redirect:"error",credentials:"omit"});
  if(!response.ok)throw new Error(a2aFailure(response.status));
  const value=await response.json();if(generation!==epoch)throw new Error("身份已改变");
  if(value.error||value.result?.isError)throw new Error("MCP操作未取得成功证据；只读恢复原回执，不重发。");
  if(initialize){
   if(value.result?.protocolVersion!=="2025-06-18")throw new Error("MCP版本不匹配");
   const notified=await fetch("/v1/isolated-mcp",{method:"POST",headers:{Authorization:`Bearer ${bearer}`,"Content-Type":"application/json",
    Accept:"application/json, text/event-stream","MCP-Protocol-Version":"2025-06-18","X-Platform-MCP-Profile":"isolated-agent-receipts-v1"},
    body:JSON.stringify({jsonrpc:"2.0",method:"notifications/initialized"}),signal:controller?.signal,cache:"no-store",redirect:"error",credentials:"omit"});
   if(generation!==epoch)throw new Error("身份已改变");if(notified.status!==202)throw new Error("MCP初始化未确认");return value.result;
  }
  return value.result?.structuredContent;
 }

 const response=await fetch(path,{method:body?"POST":"GET",headers:{Authorization:`Bearer ${bearer}`,"A2A-Version":"1.0","A2A-Extensions":extension,"Content-Type":"application/json"},
 ...(body?{body:JSON.stringify(body)}:{}),signal:controller?.signal,cache:"no-store",redirect:"error",credentials:"omit"});
 if(!response.ok)throw new Error(a2aFailure(response.status));const value=await response.json();if(generation!==epoch)throw new Error("身份已改变");return value;
}
function accept(value:Evidence,expected:string){const row=value?.receipt;
 if(value?.profile!==extension||row?.scope_id!==scope||row?.attempt_id!==expected||row.action!=="a2a_create"||
 !["PREPARED","COMMITTED","REJECTED_NO_EFFECT"].includes(row.status)||!Number.isSafeInteger(row.revision)||row.revision<1||
 !/^[a-f0-9]{64}$/.test(row.fingerprint)||typeof row.acknowledged!=="boolean"||typeof value.messageId!=="string"||typeof value.contextId!=="string"||
 (row.status==="COMMITTED"?value.taskId!==row.run_id:value.taskId!==null)||
 (row.acknowledged&&row.status==="PREPARED"))throw new Error("回执绑定不匹配");
 if(saved.value&&(saved.value.receipt.attempt_id!==expected||saved.value.receipt.fingerprint!==row.fingerprint||saved.value.messageId!==value.messageId||saved.value.contextId!==value.contextId))throw new Error("回执范围已变化");
 if(original.value&&expected===attempt&&value.messageId!==original.value.message.messageId)throw new Error("消息ID绑定不匹配");
 saved.value=value;restoreId.value=expected;preview.value=false;withdrawalPreview.value=false;
}
async function perform(action:(generation:number)=>Promise<void>,writing=false){
 if(busy.value||!applied.value)return;const generation=epoch;controller=new AbortController();busy.value=true;preview.value=false;withdrawalPreview.value=false;
 if(writing){checked.value=false;blocked.value=true;}const active=controller,timer=setTimeout(()=>active.abort(),15000);
 try{await action(generation);}catch(error){if(generation===epoch){checked.value=false;blocked.value=true;message.value=`${writing?"写结果未知或未取得终结证据":"只读核对失败"}：${String(error)}。保持锁定，不自动重发。`;}}
 finally{clearTimeout(timer);if(generation===epoch){busy.value=false;controller=null;}}
}
async function recover(){await perform(async generation=>{
 const identity=await request("/v1/workbench/identity",generation);scope=identity.scope_id;
 if(typeof scope!=="string"||identity.auth_method!=="user")throw new Error("验签身份不匹配");
 const card=await request(base+"/.well-known/agent-card.json",generation);
 if(props.mcp ? !card.capabilities?.experimental?.["isolated-agent-receipts-v1"] : !card.capabilities?.extensions?.some((v:{uri:string;required:boolean})=>v.uri===extension&&v.required))throw new Error("缺少必需扩展");
 const ledger=await request("/v1/workbench/attempts",generation);if(ledger.scope_id!==scope)throw new Error("主体锁范围不匹配");
 blocked.value=!!ledger.active;const id=ledger.active?.attempt_id||restoreId.value||attempt;
 if(ledger.active&&ledger.active.action!=="a2a_create")throw new Error("当前主体存在其他工作台未决写，请回原工作台恢复");
 if(id)accept(await request(base+"/attempts/"+encodeURIComponent(id),generation),id);else saved.value=null;
 checked.value=true;catalogReady.value=false;agents.value=[];
 message.value="已核对服务端身份、活动锁和回执；没有发送业务写。";
 try {
   const catalog=await request("/v1/isolated-discovery/agents",generation);
   if(catalog.profile!=="isolated-e1a-v1"||!Array.isArray(catalog.items))throw new Error("目录不可用");
   agents.value=catalog.items;catalogReady.value=true;
 } catch { if(generation===epoch)message.value="已核对服务端身份与回执；目录不可用，禁止新登记，仍可本人撤销登记或ACK。"; }
});}
async function prepare(){if(!checked.value||!catalogReady.value||blocked.value||!agent.value||!input.value.trim())return;
 attempt=crypto.randomUUID();attempted.value=false;saved.value=null;
 original.value={message:{messageId:crypto.randomUUID(),role:"ROLE_USER",parts:[{text:input.value}]},configuration:{returnImmediately:true}};
 await perform(async generation=>{accept(await request(base+"/prepare",generation,{...original.value,attempt_id:attempt,agent_id:agent.value}),attempt);
 checked.value=true;input.value="";message.value="登记成功，尚未创建Task；请核对后显式执行一次。";},true);
}
async function execute(){if(!checked.value||!saved.value||saved.value.receipt.status!=="PREPARED"||!original.value||attempted.value)return;
 attempted.value=true;const row=saved.value.receipt;
 await perform(async generation=>{const result=await request(base+"/message:send",generation,{...original.value,metadata:{[extension]:{attempt_id:row.attempt_id,revision:row.revision,fingerprint:row.fingerprint}}});
 accept(result.task?.metadata?.[extension],row.attempt_id);checked.value=true;original.value=null;message.value="创建已提交；Run完成仍不等于本人ACK。";},true);
}
async function ack(){if(!preview.value||!checked.value||!saved.value||saved.value.receipt.acknowledged||!["COMMITTED","REJECTED_NO_EFFECT"].includes(saved.value.receipt.status))return;
 const row=saved.value.receipt;await perform(async generation=>{accept(await request(base+"/acknowledge",generation,{attempt_id:row.attempt_id,revision:row.revision,fingerprint:row.fingerprint}),row.attempt_id);
 message.value="已收到ACK回执；请只读核对主体锁后再登记新任务。";},true);
}
async function withdraw(){
 if(!withdrawalPreview.value||!checked.value||!saved.value||saved.value.receipt.status!=="PREPARED")return;
 const row=saved.value.receipt;
 await perform(async generation=>{
   accept(await request(base+"/attempts/"+encodeURIComponent(row.attempt_id)+"/withdraw",generation,
      {attempt_id:row.attempt_id,revision:row.revision,fingerprint:row.fingerprint}),row.attempt_id);
   original.value=null;attempted.value=true;checked.value=true;
   message.value="本人撤销已取得无效果终结回执；仍保持锁定，请另行预览并确认本人ACK。";
 },true);
}
const nextStep=computed(()=>{
 if(!applied.value)return "1. 应用本人测试身份，再只读核对目录与回执。";
 if(!checked.value)return "先只读核对身份与原回执；显示的旧状态不是当前执行依据。";
 if(saved.value?.receipt.acknowledged)return "该回执已由本人确认；新任务仍须重新核对目录和主体锁。";
 if(saved.value?.receipt.status==="COMMITTED")return "Run已创建：可查看Task结果，并独立确认本人ACK；不能撤销已创建Run。";
 if(saved.value?.receipt.status==="REJECTED_NO_EFFECT")return "已取得无效果终结回执：核对后独立确认本人ACK。";
 if(saved.value?.receipt.status==="PREPARED")return original.value&&!attempted.value?"登记尚未执行：核对原消息后执行一次，或显式撤销登记。":"原消息丢失或执行情况不明：只读恢复；可显式撤销竞争，不能重发。";
 return catalogReady.value&&!blocked.value?"2. 从本身份可见目录选择Agent，填写消息并登记。":"目录不可用或主体锁定：暂不能登记新任务。";
});
watch(()=>props.mcp,()=>{reset();bearer="";token.value="";applied.value=false;});
onBeforeUnmount(()=>{reset();bearer="";token.value="";});
</script>
<template><section>
<h1>{{props.mcp?"MCP 隔离协议宿主":"A2A 隔离协议宿主"}}</h1>
<p v-if="props.mcp">平台显式回执扩展；与E2共用登记，不代表通用客户端全协议兼容。恢复查询使用MCP只读工具，HTTP POST不代表执行业务写。</p>
<router-link :to="props.mcp?'/a2a-host':'/mcp-host'">{{props.mcp?"切换A2A宿主":"打开MCP隔离宿主"}}</router-link><p>测试身份和替身专用。先核对身份与主体锁；不会自动执行、重试或ACK。</p>
<p><router-link to="/observe-inbox">第三方观测隔离收件箱（管理员）</router-link></p>
<nav aria-label="E2体验步骤">身份与目录 → 登记 → 执行或撤销 → Task结果 → 本人ACK</nav><p role="note">{{nextStep}}</p>
<label>用户访问令牌 <input v-model="token" type="password" autocomplete="off" /></label><button @click="apply">应用身份并清空旧数据</button>
<label>恢复 attempt ID（可选） <input v-model="restoreId" :disabled="busy" /></label>
<button :disabled="busy||!applied" @click="recover">只读核对身份与回执</button><p role="status">{{message}}</p>
<p v-if="checked">主体新写：{{blocked?'锁定':'可登记'}}</p>
<fieldset :disabled="busy||!checked||!catalogReady||blocked"><legend>新任务登记</legend>
<label>可见 Agent <select v-model="agent" aria-label="可见 Agent"><option value="">请选择</option><option v-for="item in agents" :key="item.agent_id" :value="item.agent_id">{{item.agent_id}} / {{item.version}}</option></select></label>
<label>消息 <textarea v-model="input" maxlength="16384" /></label><button :disabled="!agent||!input.trim()" @click="prepare">登记新任务（尚未执行）</button></fieldset>
<section v-if="saved" aria-label="A2A回执"><p>attempt：{{saved.receipt.attempt_id}}</p><p>回执状态：{{saved.receipt.status}}；revision：{{saved.receipt.revision}}</p><p>本人已确认：{{saved.receipt.acknowledged?'是':'否'}}</p>
<p v-if="saved.receipt.rejection">终结原因：{{saved.receipt.rejection}}</p>
<button v-if="saved.receipt.status==='PREPARED'" :disabled="busy||!checked" @click="withdrawalPreview=true">预览撤销登记</button>
<section v-if="withdrawalPreview" aria-label="撤销登记确认"><p>仅撤销此创建登记：{{saved.receipt.attempt_id}}。与执行竞争同一事务锁；若已创建Run则拒绝撤销。撤销成功仍须独立本人ACK。</p><button @click="withdrawalPreview=false">取消撤销登记</button><button @click="withdraw">确认本人撤销登记</button></section>
<p v-if="saved.receipt.status==='PREPARED'">登记不等于执行。仅本页仍持有原消息且未发送过执行时，才允许显式执行；重载丢失原消息或执行结果未知时仅恢复，不猜测重发。</p>
<pre v-if="original && saved.receipt.status==='PREPARED'" class="original-message">{{original.message.parts[0].text}}</pre>
<button v-if="saved.receipt.status==='PREPARED'&&original&&!attempted" :disabled="busy||!checked" @click="execute">确认执行已登记任务一次</button>
<p v-if="saved.taskId">Task ID：{{saved.taskId}} <router-link :to="{path:'/a2a-task',query:{task:saved.taskId,attempt:saved.receipt.attempt_id}}">查看此Task结果</router-link></p>
<button v-if="!saved.receipt.acknowledged&&['COMMITTED','REJECTED_NO_EFFECT'].includes(saved.receipt.status)" :disabled="busy||!checked" @click="preview=true">预览本人ACK</button>
<section v-if="preview" aria-label="ACK确认"><p>仅确认此绑定回执，不重新执行业务：{{saved.receipt.attempt_id}} / {{saved.receipt.fingerprint}}</p><button @click="preview=false">取消ACK</button><button @click="ack">确认本人ACK</button></section>
</section><p>重载后先查活动锁；若ACK响应丢失且活动锁已空，可填写原attempt ID只读核对。不得仅用空锁判断某笔写已完成。已恢复但无法确认是否发送过的PREPARED不可重发；可预览本人撤销登记，取得无效果证据后再独立ACK。不能用旧工作台接口绕过。</p>
</section></template>
<style scoped>label{display:block;margin:1rem 0}input,textarea,select{max-width:100%}section{overflow-wrap:anywhere}.original-message{white-space:pre-wrap;overflow-wrap:anywhere}</style>
