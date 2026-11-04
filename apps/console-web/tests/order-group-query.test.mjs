import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import ts from 'typescript';
const compile=file=>ts.transpileModule(readFileSync(new URL(file,import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022}}).outputText;
const url=s=>`data:text/javascript;base64,${Buffer.from(s).toString('base64')}`;
const base=url(compile('../src/order-count-query.ts'));
const query=url(compile('../src/order-group-query.ts').replace('"./order-count-query"',JSON.stringify(base)));
const {previewOrderGroups,proposeOrderGroups}=await import(query);
const {orderGroupResults}=await import(url(compile('../src/order-group-result.ts').replace('"./order-group-query"',JSON.stringify(query))));
const args={since:'2026-10-01T00:00:00Z',until:'2026-10-02T00:00:00Z',status:'completed',metric:'completed_amount',group_by:'currency'};
test('money query keeps currencies separate and explicit natural language preserves scope',()=>{
  const p=previewOrderGroups({...args,since:'2026-10-01T08:00:00+08:00'});
  assert.equal(p.request.since,'2026-10-01T00:00:00.000Z');
  assert.deepEqual(proposeOrderGroups('统计创建时间从“2026-10-01T00:00:00Z”到“2026-10-02T00:00:00Z”的已完成订单金额，按币种分组').request,p.request);
  assert.match(p.explanation.join(' '),/不换汇、不跨币种相加/);
});
for(const group_by of ['status','currency','status_currency'])test(`count grouping ${group_by}`,()=>{
  assert.equal(previewOrderGroups({...args,metric:'order_count',status:'all',group_by}).request.group_by,group_by);
});
for(const change of [{metric:'revenue'},{group_by:'customer'},{status:'all'},{group_by:'status'},
  {since:'2026-02-30T00:00:00Z'},{until:'2026-11-02T00:00:00Z'}])test(`unsupported scope ${JSON.stringify(change)}`,()=>assert.throws(()=>previewOrderGroups({...args,...change})));
for(const text of ['统计今天销售额','统计跨租户已完成订单金额','统计退款金额','统计订单利润',
  '统计创建时间从“2026-10-01T00:00:00Z”到“2026-10-02T00:00:00Z”的全部订单金额，按币种分组',
  '统计创建时间从“2026-10-01T00:00:00Z”到“2026-10-02T00:00:00Z”的已完成订单金额，按币种分组，然后发送',
  'x'.repeat(513)])test(`unsupported business expression ${text.slice(0,65)}`,()=>assert.throws(()=>proposeOrderGroups(text)));
const output={profile:'synthetic-order-groups-v1',snapshot_guaranteed:false,since:args.since,until:args.until,status_filter:'completed',metric:args.metric,group_by:args.group_by,
  view_version:'v1',policy_version:'p',read_started:'x',read_finished:'y',complete:true,reason:null,
  groups:[{key:{currency:'CNY'},order_count:2,valid_amount_count:1,known_minor:'12000',total_minor:null,amount_coverage:0.5,amount_complete:false}]};
const events=[{seq:1,type:'tool.requested',payload:{tool:'analytics.order_groups',call_id:'a',input:args}},{seq:2,type:'tool.result',payload:{call_id:'a',status:'ok',output}}];
test('partial money evidence remains unknown; exact integer strings are preserved',()=>{
  const r=orderGroupResults(events);assert.equal(r[0].value.groups[0].total_minor,null);
  const e=structuredClone(events);Object.assign(e[1].payload.output.groups[0],{order_count:1,valid_amount_count:1,known_minor:'100000000000000000001',total_minor:'100000000000000000001',amount_coverage:1,amount_complete:true});
  assert.equal(orderGroupResults(e)[0].value.groups[0].total_minor,'100000000000000000001');
  Object.assign(e[1].payload.output.groups[0],{known_minor:'0',total_minor:'0'});assert.equal(orderGroupResults(e)[0].value.groups[0].total_minor,'0');
});
test('mismatched, malformed, duplicate groups or false totals cannot render success',()=>{
  for(const mutate of [e=>e[1].payload.status='error',e=>e[1].payload.call_id='b',e=>e[1].payload.output.status_filter='all',
    e=>e[1].payload.output.group_by='customer',e=>e[1].payload.output.groups.push(e[1].payload.output.groups[0]),
    e=>e[1].payload.output.groups[0].key.currency='EUR',e=>e[1].payload.output.groups[0].total_minor='12000',
    e=>e[1].payload.output.groups[0].amount_coverage=1,e=>e[1].payload.output.groups[0].known_minor=12000,
    e=>e[1].payload.output.groups[0].known_minor='-1',e=>e[1].payload.output.groups[0].valid_amount_count=3,
    e=>e[1].payload.output.groups[0].amount_complete=true,e=>e[1].payload.output.snapshot_guaranteed=true]){
    const copy=structuredClone(events);mutate(copy);assert.deepEqual(orderGroupResults(copy),[]);
  }
});
test('incomplete source cannot expose partial groups or invent zero totals',()=>{
  const e=structuredClone(events);Object.assign(e[1].payload.output,{complete:false,reason:'来源截断'});assert.deepEqual(orderGroupResults(e),[]);
  e[1].payload.output.groups=[];assert.equal(orderGroupResults(e)[0].value.complete,false);
  Object.assign(e[1].payload.output,{complete:true,reason:null});assert.deepEqual(orderGroupResults(e)[0].value.groups,[]);
});
