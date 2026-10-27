import test from 'node:test';
import assert from 'node:assert/strict';
import { RuntimeRunClient } from '@ai-platform/sdk-web/run-client';

const row = { run_id: 'r1', subject: 'human:alice', agent_id: 'demo', state: 'completed', last_seq: 3 };
test('Run history GET explicitly scopes agent and verified subject and treats cursor as opaque', async () => {
  const client = new RuntimeRunClient(async () => ({ Authorization: 'Bearer synthetic' }), async (url, init) => {
    const path = new URL(url, 'http://synthetic');
    assert.equal(path.pathname, '/v1/runs');
    assert.equal(path.searchParams.get('subject'), row.subject);
    assert.equal(path.searchParams.get('agent_id'), row.agent_id);
    assert.equal(path.searchParams.get('cursor'), 'opaque+cursor');
    assert.equal(init.method, 'GET');
    return Response.json({ items: [row], next_cursor: null });
  });
  assert.deepEqual(await client.listOwnRuns('demo', row.subject, 'opaque+cursor'), { items: [row], next_cursor: null });
});
test('Run history rejects foreign records, duplicates, repeated cursors, invalid pages', async () => {
  for (const value of [
    { items: [{ ...row, subject: 'human:bob' }], next_cursor: null },
    { items: [{ ...row, agent_id: 'foreign' }], next_cursor: null },
    { items: [row, row], next_cursor: null },
    { items: [row], next_cursor: 'same' },
    { items: Array(21).fill(row), next_cursor: null },
    { items: [], next_cursor: '' },
  ]) {
    const client = new RuntimeRunClient(async () => ({}), async () => Response.json(value));
    await assert.rejects(client.listOwnRuns('demo', row.subject, 'same'), /不匹配/);
  }
});
