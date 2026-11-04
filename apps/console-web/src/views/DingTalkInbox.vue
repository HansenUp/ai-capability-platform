<script setup lang="ts">
import {onBeforeUnmount,onMounted,ref,watch} from "vue";
import {testTokenExpiry} from "../test-token-expiry";
interface Receipt {profile:string;receipt_id:string;event_id:string;status:string;run_created:boolean;message_sent:boolean;workbench_path:string}
const sourceDraft=ref(""), userDraft=ref(""), sender=ref("alice"), event=ref(""), text=ref("合成单聊消息"), status=ref(""), busy=ref(false), receipt=ref<Receipt|null>(null), applied=ref(false);
// Never persist tokens/text or use the source token as a user credential.
let source="",user="",epoch=0, controller:AbortController|null=null;
let expiresAt=0, expiryTimer:ReturnType<typeof setInterval>|undefined;
const attempted=new Set<string>();
const key=()=>`${sender.value}:${event.value}`;
const sent=ref(false);
function clear(){epoch++;controller?.abort();controller=null;receipt.value=null;busy.value=false;status.value="";sent.value=attempted.has(key());}
watch([sourceDraft,userDraft],()=>{clear();source="";user="";expiresAt=0;applied.value=false;},{flush:"sync"});
watch([sender,event,text],clear,{flush:"sync"});
function invalidate(message:string){clear();source="";user="";expiresAt=0;applied.value=false;text.value="";status.value=message;}
function expire(){if(applied.value&&Date.now()>=expiresAt){invalidate("测试身份已过期，已清空消息和接收结果。请重新加载并应用身份，再只读核对原事件。");return true;}return false;}
function apply(){const s=sourceDraft.value.trim(),u=userDraft.value.trim(),se=testTokenExpiry(s),ue=testTokenExpiry(u);sourceDraft.value="";userDraft.value="";clear();if(se===null||ue===null){invalidate("测试令牌缺少有效到期时间，请重新加载测试身份。");return;}source=s;user=u;expiresAt=Math.min(se,ue);applied.value=true;if(!expire())status.value="身份已应用；发送者映射仍由服务端核对。";}
async function load(){clear();const current=epoch;try{const r=await fetch('/__demo/info',{cache:'no-store',redirect:'error'});if(!r.ok)throw new Error();const v=await r.json();if(current!==epoch)return;if(!v.im_source||!v.tokens?.alice)throw new Error();sourceDraft.value=v.im_source;userDraft.value=v.tokens.alice;status.value="已加载隔离来源与Alice测试身份，请显式应用。";}catch{if(current===epoch)status.value="测试身份不可用；不改用真实凭据。";}}
async function request(write:boolean){
 if(expire())return;
 if(!applied.value||busy.value||!event.value||write&&(sent.value||!text.value))return;
 clear();const current=epoch, expected=event.value;const active=new AbortController();controller=active;busy.value=true;
 if(write){attempted.add(key());sent.value=true;}
 const timer=setTimeout(()=>active.abort(),10000);
 try{
  const params=new URLSearchParams({app_id:'demo-app',platform_tenant_id:'demo-org',sender_id:sender.value});
  const r=await fetch(write?'/v1/isolated-im/events':`/v1/isolated-im/events/${encodeURIComponent(expected)}?${params}`,{method:write?'POST':'GET',headers:{Authorization:`Bearer ${write?source:user}`,'Content-Type':'application/json'},body:write?JSON.stringify({profile:'isolated-dingtalk-v1',platform:'dingtalk',app_id:'demo-app',platform_tenant_id:'demo-org',sender_id:sender.value,event_id:expected,chat_type:'direct',message_type:'text',text:text.value}):undefined,signal:active.signal,cache:'no-store',redirect:'error',credentials:'omit'});
  if(current!==epoch)return;
  if(r.status===401){invalidate("测试身份已失效，已清空消息和接收结果。重新应用身份后只读核对，不自动重发。");return;}
  if(!r.ok)throw new Error(`HTTP ${r.status}；拒绝或无匹配记录不证明未接收，先核对身份与原事件。`);
  const v=await r.json();if(current!==epoch||expire())return;
  if(v.profile!=='isolated-dingtalk-v1'||v.event_id!==expected||v.status!=='RECEIVED'||v.run_created!==false||v.message_sent!==false||v.workbench_path!=='/chat')throw new Error('回执范围不匹配');
  // Only a successful user-authenticated GET reveals the workbench navigation.
  receipt.value=write?null:v;status.value=write?'合成消息已接收，尚未创建Run。请以本人身份只读核对。':'本人接收记录已核对；任务需在工作台重新输入并显式登记。';
 }catch(e){if(current===epoch){receipt.value=null;status.value=`${write?'接收结果未确认，不重复提交。':''}${String(e)}`;}}
 finally{clearTimeout(timer);if(current===epoch){busy.value=false;controller=null;}}
}
onMounted(()=>{expiryTimer=setInterval(expire,1000);document.addEventListener('visibilitychange',expire);window.addEventListener('focus',expire);});
onBeforeUnmount(()=>{clear();source="";user="";clearInterval(expiryTimer);document.removeEventListener('visibilitychange',expire);window.removeEventListener('focus',expire);});
</script>
<template><section>
<h1>钉钉单聊 · 隔离接收</h1>
<p>仅合成内部协议，不接真实钉钉、不发送消息。接收不是任务执行，来源令牌不是用户身份。正文不落库；工作台需本人重新输入，不自动预填或创建Run。</p>
<button @click="load">加载隔离测试身份</button>
<label>合成来源令牌<input v-model="sourceDraft" type="password" autocomplete="off" /></label>
<label>本人用户令牌<input v-model="userDraft" type="password" autocomplete="off" /></label>
<button @click="apply">应用身份并清空接收结果</button>
<label>合成发送者<select v-model="sender"><option>alice</option><option>bob</option></select></label>
<label>事件ID<input v-model="event" maxlength="128" /></label>
<label>合成消息<textarea v-model="text" maxlength="4000" /></label>
<button :disabled="!applied||busy||!event||sent||!text" @click="request(true)">显式模拟接收一次</button>
<button :disabled="!applied||busy||!event" @click="request(false)">本人只读核对原事件</button>
<p role="status">{{status}}</p>
<p>重载后填写原事件ID、发送者并重新应用本人身份，只读核对；404或断连不证明未接收。重复投递由后端去重，当前页面不会自动重发。这里没有任务ACK；既有任务未决锁仍需原操作者在工作台处理。</p>
<article v-if="receipt" aria-label="本人接收回执"><p>已接收／待本人处理 · {{receipt.event_id}}</p><p>接收ID：{{receipt.receipt_id}}</p><a href="/chat">进入工作台，重新认证并显式登记任务</a><p>链接不携带令牌或正文，不授予权限；原Run结果和同Run轨迹在工作台查看。</p></article>
</section></template>
<style scoped>label{display:block;margin:1rem 0}input,textarea{display:block;max-width:100%;width:36rem}article{padding:1rem;border:1px solid #cbd5e1;overflow-wrap:anywhere}</style>
