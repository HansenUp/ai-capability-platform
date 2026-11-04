import test from 'node:test';
import assert from 'node:assert/strict';
import {moduleUrl} from './support/compile-query-modules.mjs';
const {previewOrderCompare,proposeOrderCompare,compareCapability}=await import(moduleUrl(new URL('../src/order-compare-query.ts',import.meta.url)));
const {orderCompareResults}=await import(moduleUrl(new URL('../src/order-compare-result.ts',import.meta.url)));
const {queryCapabilities}=await import(moduleUrl(new URL('../src/query-assessment.ts',import.meta.url)));
const args={baseline_since:'2026-10-01T00:00:00Z',baseline_until:'2026-10-02T00:00:00Z',current_since:'2026-10-02T00:00:00Z',current_until:'2026-10-03T00:00:00Z',status:'all'};
test('comparison exact natural language and offsets preserve roles and leave legacy capability exports stable',()=>{
 const p=previewOrderCompare(args);assert.deepEqual(proposeOrderCompare(compareCapability.example).request,p.request);
 assert.deepEqual(previewOrderCompare({...args,baseline_since:'2026-10-01T08:00:00+08:00'}).request,p.request);
 assert.match(p.explanation.join(' '),/同一完整授权来源/);assert.match(p.explanation.join(' '),/基准为0/);
 assert.deepEqual(queryCapabilities.map(c=>c.id),['detail','count','groups']);
 for(const [name,status] of [['待完成','pending'],['已完成','completed'],['已取消','cancelled']])assert.equal(proposeOrderCompare(compareCapability.example.replace('全部',name)).request.status,status);
});
for(const change of [{current_until:'2026-10-04T00:00:00Z'},{current_since:'2026-10-01T12:00:00Z',current_until:'2026-10-02T12:00:00Z'},{current_since:'2026-09-30T00:00:00Z',current_until:'2026-10-01T00:00:00Z'},{baseline_since:'2026-02-30T00:00:00Z'},{status:'paid'},{metric:'completed_amount'},{current_status:'pending'}])test(`comparison scope rejects ${JSON.stringify(change)}`,()=>assert.throws(()=>previewOrderCompare({...args,...change})));
for(const q of [compareCapability.example+'，发送给老板',compareCapability.example.replace('全部订单数量','全部订单金额'),compareCapability.example.replace('全部','所有租户'),compareCapability.example.replace('2026-10-01T00:00:00Z','昨天')])test(`comparison question rejects ${q}`,()=>assert.throws(()=>proposeOrderCompare(q)));
const output={profile:'synthetic-order-compare-v1',...args,status_filter:'all',view_version:'v1',policy_version:'p1',snapshot_guaranteed:false,read_started:'a',read_finished:'b',complete:true,reason:null,baseline_count:3,current_count:1,delta_count:-2,relative_numerator:-2,relative_denominator:3};delete output.status;
const events=[{seq:1,type:'tool.requested',payload:{tool:'analytics.order_compare',call_id:'a',input:args}},{seq:2,type:'tool.result',payload:{call_id:'a',status:'ok',output}}];
test('comparison projection preserves exact negative, positive, zero and unknown zero-baseline ratios',()=>{
 assert.equal(orderCompareResults(events)[0].value.relative_numerator,-2);
 for(const [baseline,current] of [[0,0],[0,1],[3,0],[1,3],[2,2]]){
  const e=structuredClone(events);Object.assign(e[1].payload.output,{baseline_count:baseline,current_count:current,delta_count:current-baseline,relative_numerator:baseline?current-baseline:null,relative_denominator:baseline||null});
  assert.equal(orderCompareResults(e)[0].value.delta_count,current-baseline);
 }
});
test('comparison projection rejects partial counts, forged arithmetic, extra details and mismatched request evidence',()=>{
 for(const mutate of [e=>e[1].payload.output.delta_count=2,e=>e[1].payload.output.relative_numerator=0,e=>e[1].payload.output.relative_denominator=4,e=>e[1].payload.output.baseline_count=-1,e=>e[1].payload.output.current_count=1.5,e=>e[1].payload.output.current_count='1',e=>e[1].payload.output.order_id='private',e=>e[1].payload.output.status_filter='pending',e=>e[1].payload.output.current_until='2026-10-04T00:00:00Z',e=>e[1].payload.call_id='b',e=>e[1].payload.status='error',e=>e[0].payload.tool='analytics.order_count',e=>e[1].seq=0]){
  const e=structuredClone(events);mutate(e);assert.deepEqual(orderCompareResults(e),[]);
 }
 const e=structuredClone(events);Object.assign(e[1].payload.output,{complete:false,reason:'来源截断'});assert.deepEqual(orderCompareResults(e),[]);
 Object.assign(e[1].payload.output,{baseline_count:null,current_count:null,delta_count:null,relative_numerator:null,relative_denominator:null});assert.equal(orderCompareResults(e)[0].value.complete,false);
});
test('comparison rejects invented zero-baseline percentage and more than 100 unique source orders',()=>{
 const e=structuredClone(events);Object.assign(e[1].payload.output,{baseline_count:0,current_count:1,delta_count:1,relative_numerator:0,relative_denominator:1});assert.deepEqual(orderCompareResults(e),[]);
 Object.assign(e[1].payload.output,{baseline_count:60,current_count:60,delta_count:0,relative_numerator:0,relative_denominator:60});assert.deepEqual(orderCompareResults(e),[]);
});
