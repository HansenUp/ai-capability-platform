import test from 'node:test';
import assert from 'node:assert/strict';
import {moduleUrl} from './support/compile-query-modules.mjs';
const {previewOrderGroups,proposeOrderGroups}=await import(moduleUrl(new URL('../src/order-group-query.ts',import.meta.url)));
const {orderGroupResults}=await import(moduleUrl(new URL('../src/order-group-result.ts',import.meta.url)));
const {assessQuery}=await import(moduleUrl(new URL('../src/query-assessment.ts',import.meta.url)));
const args={since:'2026-10-01T00:00:00Z',until:'2026-10-02T00:00:00Z',status:'all',metric:'status_share',group_by:'status'};
test('share grammar and unified entry preserve the all-status denominator contract',()=>{
 const text='统计创建时间从“2026-10-01T00:00:00Z”到“2026-10-02T00:00:00Z”的订单状态占比';
 const plan=proposeOrderGroups(text);assert.deepEqual(plan.request,previewOrderGroups(args).request);
 assert.match(plan.explanation.join(' '),/空窗口占比未知/);
 assert.equal(assessQuery(text).status,'supported');assert.equal(assessQuery(text).route,'/manage/order-groups');
 for(const change of [{status:'completed'},{status:'pending'},{group_by:'currency'},{group_by:'status_currency'}])assert.throws(()=>previewOrderGroups({...args,...change}));
 assert.throws(()=>proposeOrderGroups(text+'，仅已完成'));assert.throws(()=>proposeOrderGroups(text.replace('订单状态占比','已完成订单状态占比')));
});
const output={profile:'synthetic-order-groups-v1',since:args.since,until:args.until,status_filter:'all',metric:'status_share',group_by:'status',view_version:'v1',policy_version:'p',read_started:'x',read_finished:'y',snapshot_guaranteed:false,complete:true,reason:null,total_order_count:3,
 groups:[['pending',1],['completed',2],['cancelled',0]].map(([status,n])=>({key:{status},order_count:n,share_numerator:n,share_denominator:3}))};
const events=[{seq:1,type:'tool.requested',payload:{tool:'analytics.order_groups',call_id:'a',input:args}},{seq:2,type:'tool.result',payload:{call_id:'a',status:'ok',output}}];
test('exact shares preserve zero categories, reject forged totals and mixed denominators',()=>{
 assert.equal(orderGroupResults(events)[0].value.groups[2].share_numerator,0);
 for(const mutate of [e=>e[1].payload.output.total_order_count=2,e=>e[1].payload.output.groups[0].share_denominator=1,
  e=>e[1].payload.output.groups[0].share_numerator=2,e=>e[1].payload.output.groups.pop(),
  e=>e[1].payload.output.groups[2].key.status='pending',e=>e[1].payload.output.groups[0].order_count=-1,
  e=>e[1].payload.output.status_filter='completed',e=>e[1].payload.status='error',e=>e[1].payload.call_id='foreign',
  e=>e[1].payload.output.groups[0].total_minor='1']){
  const copy=structuredClone(events);mutate(copy);assert.deepEqual(orderGroupResults(copy),[]);
 }
});
test('empty and incomplete sources cannot be promoted to zero percentages',()=>{
 const e=structuredClone(events);e[1].payload.output.total_order_count=0;
 for(const g of e[1].payload.output.groups)Object.assign(g,{order_count:0,share_numerator:null,share_denominator:null});
 assert.equal(orderGroupResults(e)[0].value.groups[0].share_denominator,null);
 e[1].payload.output.groups[0].share_denominator=0;assert.deepEqual(orderGroupResults(e),[]);
 Object.assign(e[1].payload.output,{complete:false,reason:'来源截断',groups:[],total_order_count:null});
 assert.equal(orderGroupResults(e)[0].value.complete,false);
 e[1].payload.output.total_order_count=0;assert.deepEqual(orderGroupResults(e),[]);
});
