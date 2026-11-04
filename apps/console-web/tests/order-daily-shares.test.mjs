import test from 'node:test';
import assert from 'node:assert/strict';
import {moduleUrl} from './support/compile-query-modules.mjs';
const {previewOrderGroups,proposeOrderGroups}=await import(moduleUrl(new URL('../src/order-group-query.ts',import.meta.url)));
const {orderGroupResults}=await import(moduleUrl(new URL('../src/order-group-result.ts',import.meta.url)));
const {assessQuery}=await import(moduleUrl(new URL('../src/query-assessment.ts',import.meta.url)));
const args={since:'2026-10-01T00:00:00Z',until:'2026-10-02T00:00:00Z',status:'all',metric:'status_share',group_by:'day_status',bucket_timezone:'UTC+08:00'};
const question='统计创建时间从“2026-10-01T00:00:00Z”到“2026-10-02T00:00:00Z”的订单按日状态占比，时区UTC+08:00';
test('daily shares require explicit zone and retain fixed all-status denominator in natural/unified previews',()=>{
 const p=previewOrderGroups(args);assert.deepEqual(proposeOrderGroups(question).request,p.request);assert.equal(assessQuery(question).status,'supported');assert.match(p.explanation.join(' '),/同日|日桶/);assert.equal(proposeOrderGroups(question.replace('UTC+08:00','UTC')).request.bucket_timezone,'UTC');
});
for(const change of [{status:'completed'},{status:'pending'},{status:'cancelled'},{group_by:'day'},{group_by:'day_currency'},{bucket_timezone:undefined},{bucket_timezone:'Asia/Shanghai'}])test(`daily share preview rejects ${JSON.stringify(change)}`,()=>assert.throws(()=>previewOrderGroups({...args,...change})));
for(const q of [question.replace('，时区UTC+08:00',''),question+'，然后发送',question.replace('订单按日状态占比','已完成订单按日状态占比')])test(`daily share question rejects ${q}`,()=>assert.throws(()=>proposeOrderGroups(q)));
const groups=(day,counts)=>counts.map((n,i)=>({key:{day,status:['pending','completed','cancelled'][i]},order_count:n,share_numerator:n,share_denominator:counts.reduce((a,b)=>a+b,0),bucket_since:day==='2026-10-01'?args.since:'2026-10-01T16:00:00Z',bucket_until:day==='2026-10-01'?'2026-10-01T16:00:00Z':args.until,partial_bucket:true}));
const output={profile:'synthetic-order-groups-v1',snapshot_guaranteed:false,since:args.since,until:args.until,status_filter:'all',metric:'status_share',group_by:'day_status',bucket_timezone:args.bucket_timezone,view_version:'v1',policy_version:'p',read_started:'a',read_finished:'b',complete:true,reason:null,total_order_count:6,groups:[...groups('2026-10-01',[1,3,0]),...groups('2026-10-02',[1,1,0])]};
const events=[{seq:1,type:'tool.requested',payload:{tool:'analytics.order_groups',call_id:'a',input:args}},{seq:2,type:'tool.result',payload:{call_id:'a',status:'ok',output}}];
test('daily shares validate each denominator independently and preserve zero categories',()=>{
 assert.equal(orderGroupResults(events)[0].value.groups.length,6);assert.equal(orderGroupResults(events)[0].value.groups[2].share_numerator,0);
 for(const mutate of [o=>o.groups.forEach(g=>g.share_denominator=6),o=>o.groups[0].share_denominator=3,o=>o.groups[0].share_numerator=0,o=>o.groups[0].share_denominator=null,o=>o.groups[0].share_denominator=0,o=>o.groups.pop(),o=>o.groups.push(o.groups[0]),o=>o.total_order_count=7,o=>o.groups[0].key.day='2026-10-03',o=>o.groups[0].partial_bucket=false,o=>o.groups[0].bucket_until=args.until,o=>o.groups[0].total_minor='10',o=>o.bucket_timezone='UTC']){
  const e=structuredClone(events);mutate(e[1].payload.output);assert.deepEqual(orderGroupResults(e),[]);
 }
});
test('daily shares do not manufacture empty dates or expose partial source counts',()=>{
 const e=structuredClone(events),o=e[1].payload.output;Object.assign(o,{total_order_count:0,groups:[]});assert.deepEqual(orderGroupResults(e)[0].value.groups,[]);
 o.groups=groups('2026-10-01',[0,0,0]);assert.deepEqual(orderGroupResults(e),[]);
 Object.assign(o,{complete:false,reason:'来源截断',total_order_count:null,groups:[]});assert.equal(orderGroupResults(e)[0].value.complete,false);
 o.total_order_count=0;assert.deepEqual(orderGroupResults(e),[]);o.total_order_count=null;o.groups=output.groups;assert.deepEqual(orderGroupResults(e),[]);
});
