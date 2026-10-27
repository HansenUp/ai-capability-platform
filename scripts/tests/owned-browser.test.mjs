import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { ownedBrowser, releaseExitedPipes } from '../../apps/console-web/tests/browser/owned-browser.mjs';
function fixture({ contextError, connectError, cleanupError } = {}) {
  const calls = [], child = Object.assign(new EventEmitter(), { exitCode: null, signalCode: null, stdio: [] });
  const browser = { contexts: () => [{close: async () => { calls.push('context'); if(contextError) throw contextError; }}] };
  const finish = async mode => {calls.push(mode);if(cleanupError)throw cleanupError;child.exitCode=0;child.emit('exit',0,null);};
  const server = { wsEndpoint: () => 'ws://127.0.0.1:1/test', process: () => child, close: () => finish('close'), kill: () => finish('kill') };
  const type = {launchServer: async opts => {assert.equal(opts.host,'127.0.0.1');assert.equal(opts.port,0);return server;}, connect: async () => {if(connectError)throw connectError;return browser;} };
  return {type,calls};
}
test('streams remain intact while running; exit releases all owned streams once',()=>{
  let closed=0;const child=Object.assign(new EventEmitter(),{stdio:[null,{destroy:()=>closed++},{destroy:()=>closed++}]});
  releaseExitedPipes(child);assert.equal(closed,0);child.emit('exit',0,null);assert.equal(closed,2);child.emit('exit',0,null);assert.equal(closed,2);
});
test('close contexts before normal owned shutdown; concurrent close is exactly once',async()=>{
  const f=fixture();const b=await ownedBrowser(f.type,{});await Promise.all([b.close(),b.close()]);assert.deepEqual(f.calls,['context','close']);
});
test('context failure does not prevent shutdown and remains visible',async()=>{
  const error=new Error('context failed'),f=fixture({contextError:error});const b=await ownedBrowser(f.type,{});
  await assert.rejects(b.close(),e=>e instanceof AggregateError && e.errors[0]===error);assert.deepEqual(f.calls,['context','close']);
});
test('connection failure reaps only owned server',async()=>{
  const error=new Error('connect failed'),f=fixture({connectError:error});await assert.rejects(ownedBrowser(f.type,{}),e=>e===error);assert.deepEqual(f.calls,['kill']);
});
test('primary and cleanup errors are both retained',async()=>{
  const first=new Error('connect failed'),second=new Error('kill failed'),f=fixture({connectError:first,cleanupError:second});
  await assert.rejects(ownedBrowser(f.type,{}),e=>e instanceof AggregateError && e.errors[0]===first && e.errors[1]===second);
});

test('real child exit with inherited pipes: release unblocks close without killing the pipe holder', {timeout:5000}, async()=>{
  const {spawn}=await import('node:child_process');
  const {once}=await import('node:events');
  const {setImmediate}=await import('node:timers/promises');
  const program="const {spawn}=require('node:child_process');const p=spawn(process.execPath,['-e','setInterval(()=>{},1000)'],{stdio:['ignore',1,2],detached:true});process.stdout.write(String(p.pid)+'\\n');p.unref();";
  for(const fixed of [false,true]) {
    const child=spawn(process.execPath,['-e',program],{stdio:['ignore','pipe','pipe']});
    let output='',closed=false,holder;
    child.stdout.on('data',data=>output+=data);child.once('close',()=>closed=true);
    const ended=once(child,'close');
    if(fixed)releaseExitedPipes(child);
    try {
      await once(child,'exit');holder=Number(output.trim());assert.ok(Number.isInteger(holder)&&holder>0);
      if(fixed) {await ended;assert.equal(closed,true);process.kill(holder,0);}
      else {await setImmediate();assert.equal(closed,false);}
    } finally {
      if(holder)process.kill(holder,'SIGTERM');
      await ended;
    }
  }
});
