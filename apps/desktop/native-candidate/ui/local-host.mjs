import {IsolatedDesktopSession} from './session.mjs';
const el=id=>document.getElementById(id);
const host=action=>window.__TAURI_INTERNALS__.invoke('host_command',{message:{action}});
const desktop=(action,data={})=>window.__TAURI_INTERNALS__.invoke('desktop_command',{message:{action,...data}});
let state='STOPPED',who=null,online=true,busy=false,closing=false,closePending=false,epoch=0,expiresTimer,pollTimer,authBlocked=false,startError='';
const session=new IsolatedDesktopSession({now:Date.now,verifyIdentity:()=>desktop('auth_status'),transport:{
 start:async()=>{try{state=await host('start');if(state!=='READY')throw Error(state);return Object.freeze({localRuntime:true});}catch(e){startError=String(e.message??e);throw e;}},
 stop:async()=>{state=await host('stop');if(state!=='STOPPED')throw Error(state);},
 read:()=>desktop('read')
}});
el('identity').closest('label').hidden=true;el('ttl').closest('label').hidden=true;
el('start').textContent='启动隔离 Runtime';el('login').textContent='登录本机 Keycloak';
document.querySelector('h1').textContent='本机 Keycloak + 隔离 Runtime';
el('subtitle').textContent='真实授权码 + PKCE · 实际 JWT 验签 · 临时 SQLite';
el('native-note').textContent='固定本机开发入口；无业务 Worker、模型调用或外部业务连接。';
el('scope-note').textContent='本机开发联调：身份由真实 Keycloak 验证，读取经 Runtime 授权，数据仅为临时合成会话。令牌只留在本地桥接进程内；缓存仅在内存。正式签名、公证和生产配置留到发布阶段。';
el('identity-status').textContent='未登录';
function hide(){el('result').textContent='';el('result').hidden=true;el('empty').hidden=false;}
function clear(){epoch++;clearTimeout(expiresTimer);who=null;session.logout();hide();el('identity-status').textContent='未登录或身份已失效';}
function render(){
 if(who&&Date.now()>=who.expiresAt)clear();
 el('state').textContent=state;
 el('start').disabled=busy||closing||state!=='STOPPED';el('stop').disabled=busy||closing||state!=='READY';
 el('login').disabled=busy||closing||state!=='READY'||!online;
 el('read').disabled=busy||closing||!who||(online&&state!=='READY');el('reconcile').disabled=busy;
 el('logout').disabled=busy||closing;el('network').disabled=busy||closing||state!=='READY';
 el('connection').textContent=online?'在线：读取隔离 Runtime':'离线：仅当前身份已授权的内存缓存';
 el('network').textContent=online?'模拟断网':'恢复在线';
}
async function action(fn){
 if(busy)return;busy=true;el('error').textContent='';render();
 try{await fn();}catch(error){hide();el('error').textContent=String(error.message??error);try{state=await host('status');}catch{state='UNAVAILABLE';}}
 finally{busy=false;render();if(closePending){closePending=false;void action(()=>host('close'));}}
}
async function poll(){
 const generation=epoch;
 try{
  if(state==='READY'&&!closing&&!authBlocked){
   const identity=await desktop('auth_status');
   if(generation!==epoch||closing||authBlocked||state!=='READY')return;
   if(!identity){if(who)clear();return;}
   if(Date.now()>=identity.expiresAt){clear();return;}
   if(!who||who.subject!==identity.subject||who.tenant!==identity.tenant||who.expiresAt!==identity.expiresAt){
    if(await session.authenticate(null)&&generation===epoch&&!closing&&!authBlocked){
     who=identity;el('identity-status').textContent=`Keycloak 已验签：${identity.tenant} / ${identity.username}`;
     clearTimeout(expiresTimer);expiresTimer=setTimeout(()=>{clear();render();},Math.max(0,identity.expiresAt-Date.now()));
    }
   }
  }
 }catch{if(generation===epoch&&who)clear();}
 finally{render();if(!closing)pollTimer=setTimeout(poll,300);}
}
el('start').onclick=()=>action(async()=>{state='STARTING';render();if(!await session.start())throw Error('隔离 Runtime 启动未通过：'+startError);});
el('stop').onclick=()=>{clear();authBlocked=true;void action(async()=>{try{await session.stop();}finally{authBlocked=false;epoch++;}});};
el('login').onclick=()=>{clear();authBlocked=true;void action(async()=>{try{await desktop('login');el('identity-status').textContent='请在本机 Keycloak 窗口完成登录';}finally{epoch++;authBlocked=false;}});};
el('logout').onclick=()=>{clear();authBlocked=true;void action(async()=>{try{await desktop('logout');}finally{epoch++;authBlocked=false;}});};
el('network').onclick=()=>action(async()=>{epoch++;online=!online;session.setOnline(online);await desktop('set_online',{online});});
el('read').onclick=()=>action(async()=>{hide();const generation=epoch;const value=await session.read('summary');render();if(generation===epoch&&who&&value){el('result').textContent=value.text;el('result').hidden=false;el('empty').hidden=true;}});
el('reconcile').onclick=()=>action(async()=>{state=await host('reconcile');});
el('close').onclick=()=>{closing=true;clear();clearTimeout(pollTimer);if(busy){closePending=true;render();}else void action(()=>host('close'));};
document.addEventListener('visibilitychange',()=>{if(document.hidden)hide();else render();});window.addEventListener('focus',render);
window.addEventListener('f8-close',()=>{closing=true;clear();clearTimeout(pollTimer);render();});window.addEventListener('pagehide',()=>{clear();clearTimeout(pollTimer);});
const ready=action(async()=>{state=await host('status');});
void ready.then(()=>{void poll();if(window.__F8_LOCAL_VERIFY__)void import('./local-verify.mjs');});

window.addEventListener('f8-login-cancelled',()=>{authBlocked=true;clear();render();});
window.addEventListener('f8-login-cancel-complete',()=>{epoch++;authBlocked=false;render();});
