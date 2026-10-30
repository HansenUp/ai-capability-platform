import test from "node:test";
import assert from "node:assert/strict";
import {createGuardedBridge} from "../isolated/guarded-bridge.mjs";
function fixture(){
 const window={};let caller={window,windowLabel:"main",webviewLabel:"main",source:"packaged-local",topLevel:true,capability:"isolated-host-v1"};
 let starts=0,stops=0,nonce=0;const scopes=[];
 const pipe={start:async recipe=>{starts++;assert.equal(recipe.binary,"ai-runtime-test");assert.equal(recipe.profile,"synthetic-readonly");assert.equal(recipe.environment,"empty-plus-test-allowlist");assert.deepEqual(Object.keys(recipe).sort(),["binary","environment","profile","scope"]);scopes.push(recipe.scope);return {};},handshake:async(handle,scope)=>({...scope,handle}),stop:async handle=>{stops++;return {handle,exited:true};},inspect:async handle=>({handle,exited:true})};
 const api=createGuardedBridge({context:()=>caller,mainWindow:window,pipe,challenge:()=>`test-${++nonce}`,clearIdentityAndCache:()=>{}});
 return {api,pipe,scopes,counts:()=>({starts,stops}),alter:change=>{caller={...caller,...change};}};
}
for(const change of [{window:{}},{windowLabel:"other"},{webviewLabel:"other"},{source:"remote"},{topLevel:false},{capability:"shell"}])test("reject native caller "+Object.keys(change)[0],async()=>{
 const f=fixture();f.alter(change);await assert.rejects(f.api.invoke({action:"start"}),/caller rejected/);assert.equal(f.counts().starts,0);
});
for(const field of ["origin","windowLabel","env","argv","pid","url"])test("reject payload authority "+field,async()=>{
 const f=fixture();await assert.rejects(f.api.invoke({action:"start",[field]:"main"}),/command rejected/);assert.equal(f.counts().starts,0);
});
test("fresh generation and exact pipe handshake before ready",async()=>{
 const f=fixture();assert.equal((await f.api.invoke({action:"start"})).state,"READY");await f.api.invoke({action:"stop"});
 assert.equal((await f.api.invoke({action:"start"})).state,"READY");assert.equal(f.scopes[1].generation,2);assert.notEqual(f.scopes[0].challenge,f.scopes[1].challenge);
 assert.deepEqual(Object.keys(await f.api.invoke({action:"status"})).sort(),["state","windowCloseAllowed"]);
 await f.api.invoke({action:"close"});await assert.rejects(f.api.invoke({action:"start"}),/blocked/);
});
for(const field of ["handle","generation","challenge","protocol"])test("reject mismatched handshake "+field,async()=>{
 const f=fixture();f.pipe.handshake=async(handle,scope)=>({...scope,handle,[field]:"wrong"});
 assert.equal((await f.api.invoke({action:"start"})).state,"CLEANUP_UNKNOWN");await assert.rejects(f.api.invoke({action:"start"}),/blocked/);assert.equal(f.counts().starts,1);
});
test("old handshake cannot satisfy new generation",async()=>{
 const f=fixture();let old;f.pipe.handshake=async(handle,scope)=>{old={...scope,handle};return old;};
 await f.api.invoke({action:"start"});await f.api.invoke({action:"stop"});f.pipe.handshake=async()=>old;
 assert.equal((await f.api.invoke({action:"start"})).state,"CLEANUP_UNKNOWN");assert.equal(f.counts().starts,2);
});
test("navigation during handshake withholds readiness and response",async()=>{
 const f=fixture();let resolve;f.pipe.handshake=(handle,scope)=>new Promise(r=>{resolve=()=>r({...scope,handle});});
 const pending=f.api.invoke({action:"start"});await new Promise(r=>setImmediate(r));f.alter({source:"remote"});resolve();await assert.rejects(pending,/caller rejected/);
 f.alter({source:"packaged-local"});assert.equal((await f.api.invoke({action:"status"})).state,"CLEANUP_UNKNOWN");
});

for(const mode of ["missing","extra","wrong-type","throw"])test("abnormal handshake remains locked: "+mode,async()=>{
 const f=fixture();f.pipe.handshake=async(handle,scope)=>{
  if(mode==="throw")throw new Error("synthetic pipe failure");
  if(mode==="missing")return null;
  if(mode==="extra")return {...scope,handle,verified:true};
  return {...scope,handle,generation:String(scope.generation)};
 };
 assert.equal((await f.api.invoke({action:"start"})).state,"CLEANUP_UNKNOWN");
 await assert.rejects(f.api.invoke({action:"start"}),/blocked/);assert.equal(f.counts().starts,1);assert.equal(f.counts().stops,0);
});
test("authorized close during handshake stops exactly once",async()=>{
 const f=fixture();let release;f.pipe.handshake=(handle,scope)=>new Promise(r=>{release=()=>r({...scope,handle});});
 const start=f.api.invoke({action:"start"});await new Promise(r=>setImmediate(r));
 assert.equal((await f.api.invoke({action:"close"})).windowCloseAllowed,false);release();
 assert.equal((await start).windowCloseAllowed,true);assert.equal(f.counts().stops,1);
 await assert.rejects(f.api.invoke({action:"start"}),/blocked/);
});
test("revoked caller cannot initiate close; pending handshake cannot become ready",async()=>{
 const f=fixture();let release;f.pipe.handshake=(handle,scope)=>new Promise(r=>{release=()=>r({...scope,handle});});
 const start=f.api.invoke({action:"start"});await new Promise(r=>setImmediate(r));f.alter({capability:"revoked"});
 await assert.rejects(f.api.invoke({action:"close"}),/caller rejected/);release();await assert.rejects(start,/caller rejected/);
 assert.equal(f.counts().stops,0);f.alter({capability:"isolated-host-v1"});assert.equal((await f.api.invoke({action:"status"})).state,"CLEANUP_UNKNOWN");
});
test("close followed by revocation keeps unknown; authorized read evidence closes without stop replay",async()=>{
 const f=fixture();let release;f.pipe.handshake=(handle,scope)=>new Promise(r=>{release=()=>r({...scope,handle});});
 const start=f.api.invoke({action:"start"});await new Promise(r=>setImmediate(r));await f.api.invoke({action:"close"});
 f.alter({capability:"revoked"});release();await assert.rejects(start,/caller rejected/);
 await assert.rejects(f.api.invoke({action:"reconcile"}),/caller rejected/);assert.equal(f.counts().stops,0);
 f.alter({capability:"isolated-host-v1"});assert.equal((await f.api.invoke({action:"reconcile"})).windowCloseAllowed,true);assert.equal(f.counts().stops,0);
});
test("revocation while stop is in flight does not undo exit or expose response",async()=>{
 const f=fixture();await f.api.invoke({action:"start"});let release;let stops=0;
 f.pipe.stop=handle=>{stops++;return new Promise(r=>{release=()=>r({handle,exited:true});});};
 const close=f.api.invoke({action:"close"});f.alter({source:"remote"});release();await assert.rejects(close,/caller rejected/);
 f.alter({source:"packaged-local"});assert.equal((await f.api.invoke({action:"status"})).windowCloseAllowed,true);assert.equal(stops,1);
});
