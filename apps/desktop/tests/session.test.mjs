import test from "node:test";
import assert from "node:assert/strict";
import { IsolatedDesktopSession } from "../isolated/session.mjs";
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; };
function fixture(overrides = {}) {
 let now = 100; const stopped = []; let starts = 0; let reads = 0;
 const transport = {start: async () => ({pid: ++starts}), stop: async h => {stopped.push(h);},
 read: async (_, id, identity) => {reads++; return {...identity, id, synthetic:true, text:"synthetic summary", secret:"omit"};}, ...overrides};
 const host = new IsolatedDesktopSession({now:()=>now, verifyIdentity:async subject=>({issuer:"test",tenant:"tenant",subject,expiresAt:200}),transport});
 return {host,stopped, transport, expire:()=>{now=200;}, counts:()=>({starts,reads})};
}
test("online authorized projection then offline cache only, no writes",async()=>{
 const f=fixture(); await f.host.start(); await f.host.authenticate("alice");
 assert.deepEqual(await f.host.read("one"),{id:"one",text:"synthetic summary",synthetic:true});
 f.host.setOnline(false);
 assert.equal((await f.host.read("one")).text,"synthetic summary");
 assert.equal(await f.host.read("absent"),null);
 await assert.rejects(f.host.write(),/unavailable/);
 assert.deepEqual(f.counts(),{starts:1,reads:1});
 f.expire(); assert.equal(f.host.cached("one"),null);
 await assert.rejects(f.host.read("one"),/authentication/);
 await f.host.stop(); assert.equal(f.stopped.length,1);
});
test("identity switch discards old response and cache",async()=>{
 const gate=deferred(); const f=fixture({read:async()=>gate.promise});
 await f.host.start();await f.host.authenticate("alice"); const old=f.host.read("one");
 await f.host.authenticate("bob");gate.resolve({issuer:"test",tenant:"tenant",subject:"alice",id:"one",text:"old",synthetic:true});
 assert.equal(await old,null);assert.equal(f.host.cached("one"),null);await f.host.stop();
});
test("disconnect fences pending reads without replay",async()=>{
 const gate=deferred(); const f=fixture({read:async()=>gate.promise});
 await f.host.start();await f.host.authenticate("alice");const old=f.host.read("one");f.host.setOnline(false);
 gate.resolve({issuer:"test",tenant:"tenant",subject:"alice",id:"one",text:"old",synthetic:true});
 assert.equal(await old,null);assert.equal(f.host.cached("one"),null);await f.host.stop();
});
test("stop during start disposes only late owned handle",async()=>{
 const gate=deferred();const f=fixture({start:async()=>gate.promise});
 const start=f.host.start();await assert.rejects(f.host.start(),/active/);await f.host.stop();
 const handle={pid:"owned"};gate.resolve(handle);assert.equal(await start,false);assert.deepEqual(f.stopped,[handle]);assert.equal(f.host.state,"STOPPED");
});
test("start failure does not retry; explicit restart allowed",async()=>{
 let calls=0;const f=fixture({start:async()=>{calls++;throw new Error("fail");}});
 assert.equal(await f.host.start(),false);assert.equal(f.host.state,"FAILED");assert.equal(calls,1);await f.host.stop();
});
for(const field of ["issuer","tenant","subject","id","synthetic"]) test(`reject mismatched ${field}`,async()=>{
 const f=fixture({read:async(_,id,identity)=>({...identity,id,text:"x",synthetic:true,[field]:"wrong"})});
 await f.host.start();await f.host.authenticate("alice");await assert.rejects(f.host.read("one"),/scope/);assert.equal(f.host.cached("one"),null);await f.host.stop();
});
test("reload starts without identity/cache; failed authentication clears existing cache",async()=>{
 const f=fixture();await f.host.start();await f.host.authenticate("alice");await f.host.read("one");
 assert.equal(await f.host.authenticate(""),false);assert.equal(f.host.cached("one"),null);
 const fresh=fixture();assert.equal(fresh.host.cached("one"),null);await f.host.stop();
});

test("authentication during startup does not strand lifecycle",async()=>{
 const gate=deferred();const f=fixture({start:async()=>gate.promise});const pending=f.host.start();
 await f.host.authenticate("alice");gate.resolve({pid:1});assert.equal(await pending,true);assert.equal(f.host.state,"READY");await f.host.stop();
});
test("cleanup failure retains owned handle and blocks restart",async()=>{
 const f=fixture({stop:async()=>{throw new Error("unknown");}});await f.host.start();
 await assert.rejects(f.host.stop(),/unconfirmed/);assert.equal(f.host.state,"CLEANUP_UNKNOWN");await assert.rejects(f.host.start(),/active/);
});
test("late auth verification cannot restore logged out identity",async()=>{
 const gate=deferred();const host=new IsolatedDesktopSession({now:()=>100,verifyIdentity:()=>gate.promise,transport:{}});
 const pending=host.authenticate("test");host.logout();gate.resolve({issuer:"test",tenant:"tenant",subject:"alice",expiresAt:200});assert.equal(await pending,false);
 await assert.rejects(host.read("one"),/authentication/);
});

test("late start cleanup failure is visible and prevents another process",async()=>{
 const gate=deferred();const f=fixture({start:()=>gate.promise,stop:async()=>{throw new Error("lost");}});
 const pending=f.host.start();await f.host.stop();assert.equal(f.host.state,"STOPPING");await assert.rejects(f.host.start(),/active/);
 gate.resolve({pid:1});assert.equal(await pending,false);assert.equal(f.host.state,"CLEANUP_UNKNOWN");await assert.rejects(f.host.start(),/active/);
});
