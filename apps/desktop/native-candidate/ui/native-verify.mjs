// Runs only in the CLI-selected native verification window. Uses real Tauri IPC.
const cases=[];
const $=id=>document.getElementById(id);
const invoke=(command,args)=>window.__TAURI_INTERNALS__.invoke(command,args);
const assert=(value,message)=>{if(!value)throw Error(message);};
async function until(fn){const end=Date.now()+5000;while(!fn()){if(Date.now()>end)throw Error('condition timeout');await new Promise(r=>setTimeout(r,20));}}
async function click(id){$(id).click();await new Promise(r=>setTimeout(r,30));}
try{
 assert($('state').textContent==='STOPPED','initial state');
 await click('start');await until(()=>$('state').textContent==='READY');cases.push('real_ipc_start_and_pipe_handshake');
 for(const message of [{action:'exec'},{action:'start',path:'/bin/sh'},{action:'stop',pid:1}]){
  let rejected=false;try{await invoke('host_command',{message});}catch{rejected=true;}assert(rejected,'schema accepted forbidden authority');
 }
 let rejected=false;try{await invoke('plugin:shell|execute',{program:'/bin/true'});}catch{rejected=true;}assert(rejected,'shell was allowed');cases.push('real_ipc_unknown_action_extra_path_pid_and_shell_rejected');
 await click('login');await click('read');await until(()=>!$('result').hidden);assert($('result').textContent.includes('alice'),'alice text');
 await click('network');await click('read');assert(!$('result').hidden,'offline cache missing');cases.push('native_window_online_read_then_offline_cache');
 $('identity').value='bob';$('identity').dispatchEvent(new Event('change'));assert($('result').textContent==='','identity switch did not clear');
 await click('login');await click('read');assert($('result').hidden,'cross identity cache');await click('network');await click('read');await until(()=>!$('result').hidden);assert($('result').textContent.includes('bob'),'bob text');cases.push('native_window_identity_switch_isolation');
 $('ttl').value='1';await click('login');await click('read');await until(()=>!$('result').hidden);await until(()=>$('result').hidden);assert($('result').textContent==='','expiry did not clear');cases.push('native_window_natural_expiry');
 await click('stop');await until(()=>$('state').textContent==='STOPPED');assert($('read').disabled,'read after stop');
 await click('start');await until(()=>$('state').textContent==='READY');cases.push('native_window_stop_and_restart');
 await invoke('verification_report',{report:{passed:true,cases}});
}catch(error){cases.push('FAIL: '+String(error.message??error).slice(0,400));await invoke('verification_report',{report:{passed:false,cases}});}
