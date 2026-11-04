import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import ts from 'typescript';
const compile = file => ts.transpileModule(readFileSync(new URL(file, import.meta.url), 'utf8'), {
  compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022},
}).outputText;
const url = s => `data:text/javascript;base64,${Buffer.from(s).toString('base64')}`;
const queryUrl = url(compile('../src/order-count-query.ts'));
const {previewOrderCount,proposeOrderCount} = await import(queryUrl);
const {orderCountResults} = await import(url(compile('../src/order-count-result.ts').replace('"./order-count-query"', JSON.stringify(queryUrl))));
const args = {since:'2026-10-01T00:00:00Z',until:'2026-10-02T00:00:00Z',status:'all'};
test('explicit timezone conversion and fixed natural language status yield same query', () => {
  const p = previewOrderCount({...args,since:'2026-10-01T08:00:00+08:00'});
  assert.equal(p.request.since,'2026-10-01T00:00:00.000Z');
  assert.deepEqual(proposeOrderCount('统计创建时间从“2026-10-01T00:00:00Z”到“2026-10-02T00:00:00Z”的已完成订单数量').request, {...p.request,status:'completed'});
  assert.match(p.explanation.join(' '), /左闭右开/);
  assert.match(p.explanation.join(' '), /不返回订单明细/);
  assert.doesNotThrow(() => previewOrderCount({...args,until:'2026-11-01T00:00:00Z'}));
});
for (const change of [
  {since:'2026-02-30T00:00:00Z'},{since:'2026-10-01'},{since:'2026-10-01T00:00:00'},
  {since:'2026-10-01T24:00:00Z'},{since:'2026-10-01T00:00:00+14:30'},
  {since:'2026-10-01T00:00:00+15:00'},{since:'2026-10-01T00:00:00+00:60'},{until:args.since},{until:'2026-11-01T00:00:00.001Z'},
  {status:'paid'},
]) test(`invalid date/status scope: ${JSON.stringify(change)}`, () => assert.throws(() => previewOrderCount({...args,...change})));
for (const text of ['统计今天的订单数量','统计所有租户订单数量','统计订单销售额',
  '统计创建时间从“2026-10-01T00:00:00Z”到“2026-10-02T00:00:00Z”的已完成订单数量，然后发送',
  '统计创建时间从“2026-10-01T00:00:00Z”到“2026-10-02T00:00:00Z”的已支付订单数量', 'x'.repeat(513)]) {
  test(`unsupported business semantics rejected: ${text.slice(0,80)}`, () => assert.throws(() => proposeOrderCount(text)));
}
const output = {profile:'synthetic-order-count-v1',snapshot_guaranteed:false,...args,
  status_filter:'all',view_version:'v1',policy_version:'p1',read_started:'x',read_finished:'y',
  complete:true,reason:null,order_count:2,statuses:{pending:1,completed:1,cancelled:0}};
const events = [{seq:1,type:'tool.requested',payload:{tool:'analytics.order_count',call_id:'a',input:args}},
  {seq:2,type:'tool.result',payload:{call_id:'a',status:'ok',output}}];
test('result projection requires exact request, matched tool evidence and consistent totals', () => {
  assert.equal(orderCountResults(events)[0].value.order_count,2);
  for (const mutate of [e=>e[1].payload.status='error',e=>e[1].payload.call_id='b',e=>e[1].payload.output.status_filter='pending',
    e=>e[1].payload.output.until='2026-10-03T00:00:00Z',e=>e[1].payload.output.order_count=3,
    e=>e[1].payload.output.statuses.pending=-1,e=>e[1].payload.output.statuses.other=0,
    e=>e[1].payload.output.snapshot_guaranteed=true,e=>e[1].seq=0]) {
    const copy=structuredClone(events);mutate(copy);assert.deepEqual(orderCountResults(copy),[]);
  }
  const unknown=structuredClone(events);Object.assign(unknown[1].payload.output,{complete:false,reason:'来源截断',order_count:null,statuses:null});
  assert.equal(orderCountResults(unknown)[0].value.order_count,null);
  unknown[1].payload.output.order_count=0;assert.deepEqual(orderCountResults(unknown),[]);
});
