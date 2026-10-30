import test from "node:test";
import assert from "node:assert/strict";
import {NativeBridgeModel} from "../isolated/native-bridge.mjs";
function fixture(overrides={}) {
 const handle=Object.freeze({generation:"synthetic-1"});let starts=0,stops=0,clears=0;
 const driver={start:async recipe=>{starts++;assert.deepEqual(recipe,{binary:"ai-runtime-test",network:"loopback-only",environment:"synthetic-only"});return handle;},stop:async h=>{stops++;assert.equal(h,handle);return {handle:h,exited:true};},inspect:async()=>({handle,exited:true}),...overrides};
 return {bridge:new NativeBridgeModel({driver,clearIdentityAndCache:()=>{clears++;}}),handle,counts:()=>({starts,stops,clears})};
}
for(const message of [{action:"start",path:"/bin/sh"},{action:"stop",pid:1},{action:"start",env:{KEY:"x"}},{action:"exec"},{action:"start",url:"https://external"},null]) test("reject frontend override "+JSON.stringify(message),async()=>{
 const f=fixture();await assert.rejects(f.bridge.command(message),/rejected/);assert.equal(f.counts().starts,0);
});
test("close clears identity and waits for exact owned exit",async()=>{
 let finish;const f=fixture({stop:h=>new Promise(resolve=>{finish=()=>resolve({handle:h,exited:true});})});
 await f.bridge.command({action:"start"});const closing=f.bridge.command({action:"close"});
 assert.equal(f.counts().clears,1);assert.deepEqual(f.bridge.status(),{state:"STOPPING",windowCloseAllowed:false});
 await assert.rejects(f.bridge.command({action:"start"}),/blocked/);finish();assert.equal((await closing).windowCloseAllowed,true);
});
test("foreign exit cannot release; reconcile never repeats stop",async()=>{
 let stops=0;const f=fixture({stop:async()=>{stops++;return {handle:{generation:"synthetic-1"},exited:true};}});
 await f.bridge.command({action:"start"});await assert.rejects(f.bridge.command({action:"close"}),/unknown/);
 await assert.rejects(f.bridge.command({action:"stop"}),/reconciliation/);assert.equal(stops,1);
 assert.equal((await f.bridge.command({action:"reconcile"})).windowCloseAllowed,true);assert.equal(stops,1);
});
test("close during start cleans late owned handle without reopening",async()=>{
 let finish;const f=fixture({start:()=>new Promise(resolve=>{finish=resolve;})});
 const starting=f.bridge.command({action:"start"});await f.bridge.command({action:"close"});assert.equal(f.bridge.status().windowCloseAllowed,false);
 finish(f.handle);await starting;assert.equal(f.counts().stops,1);assert.equal(f.bridge.status().windowCloseAllowed,true);
});
test("ambiguous start failure stays blocked, no automatic retry",async()=>{
 const f=fixture({start:async()=>{throw new Error("unknown spawn outcome");}});
 await f.bridge.command({action:"start"});assert.equal(f.bridge.status().state,"CLEANUP_UNKNOWN");
 await assert.rejects(f.bridge.command({action:"start"}),/blocked/);await assert.rejects(f.bridge.command({action:"reconcile"}),/attributable/);
});

test("adopted internal handle is not exposed through status",async()=>{
 const handle={generation:"owned"};let stopped;
 const bridge=new NativeBridgeModel({ownedHandle:handle,clearIdentityAndCache:()=>{},driver:{stop:async h=>{stopped=h;return {handle:h,exited:true};}}});
 assert.deepEqual(bridge.status(),{state:"READY",windowCloseAllowed:false});
 assert.equal((await bridge.command({action:"close"})).windowCloseAllowed,true);assert.equal(stopped,handle);
});
