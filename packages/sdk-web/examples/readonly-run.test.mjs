import test from 'node:test';
import assert from 'node:assert/strict';
import { mountReadonlyRun } from './readonly-run.ts';

const run = { run_id: 'synthetic', agent_id: 'demo', subject: 'human:alice', state: 'working', last_seq: 2 };
const row = seq => ({ run_id: run.run_id, seq, type: 'model.response', payload: { text: 'synthetic' } });
test('host callbacks follow one Run from the last inspected event, without writes', async () => {
  const received = [];
  const client = {
    async inspect(id, signal) { assert.equal(id, run.run_id); assert.equal(signal.aborted, false); return { run, events: [row(2)] }; },
    async *follow(id, cursor) { assert.equal(id, run.run_id); assert.equal(cursor, 2); yield row(3); },
  };
  const view = mountReadonlyRun(client, run.run_id, {
    snapshot: (value, events) => received.push([value, events]), event: value => received.push(value), error: () => assert.fail(),
  });
  await view.finished;
  assert.equal(received.length, 2); assert.equal(received[1].seq, 3);
  view.close();
});
test('closed host suppresses ignored-abort old responses and never starts follow', async () => {
  let release;
  const delayed = new Promise(resolve => { release = resolve; });
  const client = { inspect: () => delayed, async *follow() { assert.fail('Old identity must not follow'); } };
  const view = mountReadonlyRun(client, run.run_id, { snapshot: () => assert.fail(), event: () => assert.fail(), error: () => assert.fail() });
  view.close(); release({ run, events: [] }); await view.finished;
});
test('host shows controlled error once without reconnect', async () => {
  let attempts = 0, errors = 0;
  const client = { async inspect() { attempts++; throw new Error('private server body'); } };
  const view = mountReadonlyRun(client, run.run_id, { snapshot: () => assert.fail(), event: () => assert.fail(), error: () => { errors++; } });
  await view.finished; assert.equal(attempts, 1); assert.equal(errors, 1); view.close();
});
