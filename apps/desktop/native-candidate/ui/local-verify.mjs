// Actual Tauri windows, Keycloak forms and Runtime/JWT/SQLite; disposable users only.
const el=id=>document.getElementById(id),cases=[];
const dc=(action,data={})=>window.__TAURI_INTERNALS__.invoke('desktop_command',{message:{action,...data}});
const assert=(value,message)=>{if(!value)throw Error(message);};
async function until(fn,timeout=20000){const end=Date.now()+timeout;while(!fn()){if(Date.now()>end)throw Error('timeout: '+el('error').textContent);await new Promise(r=>setTimeout(r,30));}}
async function click(id){await until(()=>!el(id).disabled);el(id).click();await new Promise(r=>setTimeout(r,40));}
try{
 await click('start');await until(()=>el('state').textContent==='READY');
 const diagnostics=await dc('diagnostics');assert(diagnostics.real_jwks&&diagnostics.unauthenticated_status===401&&diagnostics.forged_headers_status===401&&!diagnostics.worker_started,'Runtime isolation');cases.push('native launches fixed isolated Runtime, real JWT required, no worker');
 let rejected=false;try{await window.__TAURI_INTERNALS__.invoke('desktop_command',{message:{action:'read',token:'forged'}});}catch{rejected=true;}assert(rejected,'credential injection accepted');cases.push('real native IPC rejects injected token');
 await click('login');await until(()=>el('identity-status').textContent.includes('default / f8-alice'));await click('read');await until(()=>!el('result').hidden);assert(el('result').textContent.includes('1 条'),'Alice ownership');cases.push('native Keycloak login window, authorization code/PKCE/id-token nonce, Runtime reads Alice row');
 const before=(await dc('diagnostics')).runtime_reads;await click('network');await click('read');assert(!el('result').hidden&&el('result').textContent.includes('1 条'),'offline cached content');assert((await dc('diagnostics')).runtime_reads===before,'offline made a Runtime read');cases.push('offline memory cache performs no Runtime read');
 await click('network');await click('login');assert(el('result').textContent==='','switch did not clear');await until(()=>el('identity-status').textContent.includes('t2 / f8-bob'));await click('read');await until(()=>!el('result').hidden);assert(el('result').textContent.includes('2 条'),'Bob ownership');cases.push('native second-user login isolates tenant/subject and clears previous cache');
 await until(()=>el('result').hidden&&el('read').disabled,25000);assert(el('result').textContent==='','expiry retained content');cases.push('actual Keycloak expiry clears native window without user action');
 await click('logout');assert(el('result').hidden,'logout cache');await click('stop');await until(()=>el('state').textContent==='STOPPED');await click('start');await until(()=>el('state').textContent==='READY');cases.push('logout, precise Runtime stop and fresh restart');
 await dc('login');cases.push('close while a new Keycloak login is pending');
 await window.__TAURI_INTERNALS__.invoke('verification_report',{report:{passed:true,cases}});
}catch(error){cases.push('FAIL: '+String(error.message??error).slice(0,400));await window.__TAURI_INTERNALS__.invoke('verification_report',{report:{passed:false,cases}});}
