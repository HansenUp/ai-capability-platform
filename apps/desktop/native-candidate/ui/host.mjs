import {IsolatedDesktopSession} from './session.mjs';
const el=id=>document.getElementById(id);
const invoke=action=>window.__TAURI_INTERNALS__.invoke('host_command',{message:{action}});
let online=true,who='',expires=0,timer,closing=false,busy=false,epoch=0,native='STOPPED',closePending=false;
const session=new IsolatedDesktopSession({now:Date.now,
 verifyIdentity:async subject=>({issuer:'synthetic-desktop',tenant:'demo',subject,expiresAt:expires}),
 transport:{start:async()=>{native=await invoke('start');if(native!=='READY')throw Error(native);return Object.freeze({synthetic:true});},
 stop:async()=>{native=await invoke('stop');if(!['STOPPED','CLOSED'].includes(native))throw Error(native);},
 read:async(_,id,identity)=>{if(await invoke('status')!=='READY')throw Error('原生进程未就绪');return {...identity,id,synthetic:true,text:`${identity.subject} 的合成摘要：3 项待跟进，无真实业务数据。`};}}});
function hide(){el('result').hidden=true;el('result').textContent='';el('empty').hidden=false;}
function clear(){epoch++;clearTimeout(timer);who='';expires=0;session.logout();hide();el('identity-status').textContent='未认证或身份已失效';}
function render(){if(who&&Date.now()>=expires)clear();el('state').textContent=native;el('start').disabled=busy||closing||!['STOPPED'].includes(native);el('stop').disabled=busy||closing||native!=='READY';el('login').disabled=busy||closing||native!=='READY';el('read').disabled=busy||closing||!who||(online&&native!=='READY');el('reconcile').disabled=busy;}
async function action(fn){if(busy)return;busy=true;el('error').textContent='';render();try{await fn();}catch(e){hide();el('error').textContent=String(e.message??e);try{native=await invoke('status');}catch{native='UNAVAILABLE';}}finally{busy=false;render();if(closePending){closePending=false;void action(async()=>{native=await invoke('close');});}}}
el('start').onclick=()=>action(()=>session.start().then(ok=>{if(!ok)throw Error('原生启动未通过');}));
el('stop').onclick=()=>{clear();void action(()=>session.stop());};
el('reconcile').onclick=()=>action(async()=>{native=await invoke('reconcile');});
el('close').onclick=()=>{closing=true;clear();if(busy){closePending=true;render();}else void action(async()=>{native=await invoke('close');});};
el('identity').onchange=()=>{clear();render();};el('logout').onclick=()=>{clear();render();};
el('login').onclick=()=>action(async()=>{clear();const ttl=Number(el('ttl').value);if(!Number.isInteger(ttl)||ttl<1||ttl>300)throw Error('有效期需为1至300秒');expires=Date.now()+ttl*1000;if(await session.authenticate(el('identity').value)){who=el('identity').value;el('identity-status').textContent=`测试身份：demo / ${who}`;timer=setTimeout(()=>{clear();render();},ttl*1000);}});
el('network').onclick=()=>{epoch++;online=!online;session.setOnline(online);el('connection').textContent=online?'在线合成数据':'离线：仅已授权合成缓存';el('network').textContent=online?'模拟断网':'恢复在线';render();};
el('read').onclick=()=>action(async()=>{hide();const generation=epoch;const value=await session.read('summary');render();if(generation===epoch&&who&&value){el('result').textContent=value.text;el('result').hidden=false;el('empty').hidden=true;}});
document.addEventListener('visibilitychange',()=>{if(document.hidden)hide();else render();});window.addEventListener('focus',render);window.addEventListener('pagehide',clear);
window.addEventListener('f8-close',()=>{closing=true;clear();render();});
const ready=action(async()=>{native=await invoke('status');});
if(window.__F8_VERIFY__)void ready.then(()=>import('./native-verify.mjs'));
