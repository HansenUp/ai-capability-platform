import { NativeBridgeModel } from "./native-bridge.mjs";
import { IsolatedDesktopSession } from "./session.mjs";
const el = id => document.getElementById(id);
let expiry = 0, identity = "", online = true, timer, epoch = 0, sequence = 0;
let bridge = null, closeIntent = false, ownedHandle = null, startPromise = null, stops = 0, inspections = 0;
let pending = null;
async function hold(){if(pending)throw new Error("已有暂挂替身");el("release").disabled=false;el("pending").textContent="替身操作暂挂";await new Promise(resolve=>{pending=resolve;});}
el("release").onclick=()=>{const release=pending;pending=null;el("release").disabled=true;el("pending").textContent="无暂挂操作";release?.();};
const host = new IsolatedDesktopSession({now: Date.now,
 verifyIdentity: async subject => ({issuer:"synthetic-desktop",tenant:"demo",subject,expiresAt:expiry}),
 transport: {start:async()=>{if(el("fault").value==="hold-start")await hold();if(el("fault").value==="start")throw new Error("synthetic start failure");ownedHandle={id:++sequence};return ownedHandle;},
 stop:async()=>{if(el("fault").value==="stop")throw new Error("synthetic cleanup unknown");},
 read:async(_,id,who)=>{const fault=el("fault").value;if(fault.startsWith("hold-read"))await hold();if(fault==="hold-read-error")throw new Error("旧身份读取失败");if(el("fault").value==="read")throw new Error("合成读取失败；未发起重试");return {...who,id,synthetic:true,text:`${who.subject} 的合成工单摘要：3 项待跟进，无真实工单数据。`};}}});
function hide(){el("result").textContent="";el("result").hidden=true;el("empty").hidden=false;}
function clearIdentity(){epoch++;clearTimeout(timer);identity="";expiry=0;host.logout();hide();el("identity-status").textContent="未认证或身份已失效";}
function render(){
 if(identity && Date.now()>=expiry)clearIdentity();
 el("state").textContent=host.state;
 el("start").disabled=!["STOPPED","FAILED"].includes(host.state);
 el("stop").disabled=["STOPPED","STOPPING"].includes(host.state);
 el("lifecycle-note").textContent=host.state==="CLEANUP_UNKNOWN"?"清理结果未知，禁止新启动；仅可显式核对自有替身清理。":host.state==="FAILED"?"启动失败，未自动重试。":"";
 const status=bridge?.status();
 el("close-state").textContent=closeIntent ? (status?.windowCloseAllowed ? "退出证据匹配：允许关闭（仅模拟）" : `关闭待核对：${status?.state ?? host.state}`) : "未请求关闭";
 el("reconcile").disabled=!closeIntent || status?.state!=="CLEANUP_UNKNOWN";
 el("calls").textContent=`停止调用：${stops} · 核对调用：${inspections}`;
 if(closeIntent){el("start").disabled=true;el("stop").disabled=true;el("login").disabled=true;}
 el("read").disabled=closeIntent || !identity || (online && host.state!=="READY");
}
async function action(fn,isolate=false){const current=epoch;el("error").textContent="";try{const pending=fn();render();await pending;}catch(e){if(!isolate || current===epoch){hide();el("error").textContent=e.message;}}finally{render();}}
el("start").onclick=()=>action(async()=>{startPromise=host.start();await startPromise;startPromise=null;});
async function closeHost(){
 if(!closeIntent){closeIntent=true;clearIdentity();}
 render();
 if(startPromise)await startPromise;
 if(!bridge){
  bridge=new NativeBridgeModel({ownedHandle,clearIdentityAndCache:clearIdentity,driver:{
   stop:async handle=>{stops++;await host.stop();return {handle,exited:true};},
   inspect:async handle=>{inspections++;const mode=el("proof").value;return mode==="absent"?null:{handle:mode==="foreign"?{id:handle.id}:handle,exited:mode==="matched"};}
  }});
 }
 await bridge.command({action:"close"});
}
el("close-intent").onclick=()=>action(closeHost);
el("reconcile").onclick=()=>action(()=>bridge.command({action:"reconcile"}));
el("stop").onclick=()=>action(()=>{clearIdentity();return host.stop();});
el("identity").onchange=()=>{clearIdentity();render();};
el("logout").onclick=()=>{clearIdentity();render();};
el("login").onclick=()=>action(async()=>{
 clearIdentity();const current=epoch;const seconds=Number(el("ttl").value);
 if(!Number.isInteger(seconds)||seconds<1||seconds>300)throw new Error("有效期需为1至300秒");
 expiry=Date.now()+seconds*1000;
 if(await host.authenticate(el("identity").value) && current===epoch){identity=el("identity").value;el("identity-status").textContent=`测试身份：demo / ${identity}`;timer=setTimeout(()=>{clearIdentity();render();},Math.max(0,expiry-Date.now()));}
});
el("network").onclick=()=>{epoch++;online=!online;host.setOnline(online);el("connection").textContent=online?"在线替身":"离线：仅已授权合成缓存";el("network").textContent=online?"模拟断网":"恢复在线替身";render();};
el("read").onclick=()=>action(async()=>{hide();const current=epoch;const result=await host.read("summary");if(current!==epoch)return;render();if(identity && result){el("result").textContent=result.text;el("result").hidden=false;el("empty").hidden=true;}},true);
// A throttled/background tab must recheck before showing cached content again.
document.addEventListener("visibilitychange",()=>{if(document.hidden)hide();else render();});
window.addEventListener("focus",render);
window.addEventListener("pagehide",()=>{clearIdentity();if(!closeIntent)void host.stop().catch(()=>{});});
render();
