import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter, once} from 'node:events';
import {spawn} from 'node:child_process';
import {cleanupDemo, closeOwnedServer, stopOwnedChild} from '../demo-cleanup.mjs';

function child({exitOn = 'SIGINT', code = 0} = {}) {
  const value = Object.assign(new EventEmitter(), {pid: 123, exitCode: null, signalCode: null, signals: []});
  value.kill = signal => {
    value.signals.push(signal);
    if (signal === exitOn) {value.exitCode = code; value.emit('exit', code, null);}
    return true;
  };
  return value;
}

test('owned child graceful exit is confirmed before cleanup proceeds', async () => {
  const owned = child();
  assert.deepEqual(await stopOwnedChild(owned), {stopped: true, forced: false});
  assert.deepEqual(owned.signals, ['SIGINT']);
  assert.equal(owned.listenerCount('exit'), 0);
  assert.equal(owned.listenerCount('error'), 0);
});

test('unconfirmed child is bounded and never falsely reported stopped', async () => {
  const owned = child({exitOn: null});
  await assert.rejects(stopOwnedChild(owned, {graceMs: 5, terminateMs: 5, killMs: 5}), /exit is unconfirmed/);
  assert.deepEqual(owned.signals, ['SIGINT', 'SIGTERM', 'SIGKILL']);
  assert.equal(owned.listenerCount('exit'), 0);
  assert.equal(owned.listenerCount('error'), 0);
});

test('real unresponsive owned child is reaped through its ChildProcess object', {timeout: 5000}, async () => {
  const owned = spawn(process.execPath, ['-e', "process.on('SIGINT',()=>{});process.on('SIGTERM',()=>{});console.log('ready');setInterval(()=>{},1000);"], {stdio: ['ignore', 'pipe', 'pipe']});
  try {
    await once(owned.stdout, 'data');
    assert.deepEqual(await stopOwnedChild(owned, {graceMs: 10, terminateMs: 10, killMs: 1000}), {stopped: true, forced: true});
    assert.equal(owned.signalCode, 'SIGKILL');
  } finally {
    if (owned.exitCode === null && owned.signalCode === null) {const ended = once(owned, 'exit'); owned.kill('SIGKILL'); await ended;}
    owned.stdout.destroy(); owned.stderr.destroy();
  }
});

test('a server close failure still stops the fixture and saves evidence; active files are retained', async () => {
  const fixture = child(), calls = [];
  const result = await cleanupDemo({fixture,
    portal: {close: async () => {calls.push('portal'); throw new Error('synthetic close failure');}},
    server: {close: async () => {calls.push('console');}},
    writeEvidence: async () => {calls.push('evidence');},
    removeTemporary: async () => {calls.push('remove');},
  });
  assert.deepEqual(calls, ['portal', 'console', 'evidence']);
  assert.equal(fixture.exitCode, 0);
  assert.equal(result.resourcesStopped, false);
  assert.equal(result.removed, false);
  assert.equal(result.errors[0].cause.message, 'synthetic close failure');
});

test('HTTP connections are force-closed only after the owned server grace period', async () => {
  let release;
  const closed = new Promise(resolve => {release = resolve;});
  let forced = 0;
  const server = {close: () => closed, httpServer: {closeAllConnections: () => {forced++; release();}}};
  assert.deepEqual(await closeOwnedServer(server, {graceMs: 5, forceMs: 100}), {forced: true});
  assert.equal(forced, 1);
});

test('a server that never confirms close has a bounded failure', async () => {
  const server = {close: () => new Promise(() => {})};
  await assert.rejects(closeOwnedServer(server, {graceMs: 5, forceMs: 5}), /close is unconfirmed/);
});

test('evidence write failure does not strand fixture but retains diagnostic files', async () => {
  const fixture = child(); let removed = false;
  const result = await cleanupDemo({fixture,
    writeEvidence: async () => {throw new Error('disk full');},
    removeTemporary: async () => {removed = true;},
  });
  assert.equal(fixture.exitCode, 0);
  assert.equal(result.resourcesStopped, true);
  assert.equal(result.removed, false);
  assert.equal(removed, false);
  assert.equal(result.errors[0].cause.message, 'disk full');
});

test('unexpected fixture exit is reported even when its temporary root can be removed', async () => {
  const fixture = child({code: 7});
  const result = await cleanupDemo({fixture, writeEvidence: async () => {}, removeTemporary: async () => {}});
  assert.equal(result.resourcesStopped, true);
  assert.equal(result.removed, true);
  assert.match(result.errors[0].message, /exited with code 7/);
});

test('failed spawn and already exited children do not receive new signals', async () => {
  const failed = child(); failed.pid = undefined;
  const ended = child(); ended.exitCode = 0;
  await stopOwnedChild(failed); await stopOwnedChild(ended);
  assert.deepEqual(failed.signals, []); assert.deepEqual(ended.signals, []);
});
