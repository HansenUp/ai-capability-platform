import test from 'node:test';
import assert from 'node:assert/strict';
import { readAgents, searchAgents } from '../src/agent-catalog.ts';

const rows = [{ agent_id: 'Sales', version: '1', rollout: 'stable' }];
test('search only narrows authorized Runtime rows', () => {
  assert.deepEqual(searchAgents(rows, ' SALE '), rows);
  assert.deepEqual(searchAgents(rows, 'hidden'), []);
});
test('catalog uses readonly no-store bearer request', async () => {
  const signal = new AbortController().signal;
  assert.deepEqual(await readAgents('synthetic', signal, async (url, init) => {
    assert.equal(url, '/v1/agents');
    assert.equal(init.method, undefined);
    assert.equal(init.credentials, 'omit');
    assert.equal(init.redirect, 'error');
    assert.equal(init.cache, 'no-store');
    assert.equal(init.headers.authorization, 'Bearer synthetic');
    assert.equal(init.signal, signal);
    return Response.json(rows);
  }), rows);
});
test('HTTP errors and duplicate or malformed rows fail closed', async () => {
  const signal = new AbortController().signal;
  await assert.rejects(readAgents('test', signal, async () => new Response('', { status: 403 })), /403/);
  for (const body of [[...rows, ...rows], {}, [{ agent_id: 'x' }]]) {
    await assert.rejects(readAgents('test', signal, async () => Response.json(body)), /格式无效/);
  }
});
test('late response after cancellation cannot become catalog data', async () => {
  const controller = new AbortController();
  await assert.rejects(readAgents('test', controller.signal, async () => {
    controller.abort();
    return Response.json(rows);
  }), { name: 'AbortError' });
});
