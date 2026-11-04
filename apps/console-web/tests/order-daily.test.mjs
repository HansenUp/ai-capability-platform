import test from 'node:test';
import assert from 'node:assert/strict';
import {moduleUrl} from './support/compile-query-modules.mjs';
const {previewOrderGroups,proposeOrderGroups}=await import(moduleUrl(new URL('../src/order-group-query.ts',import.meta.url)));
const {orderGroupResults}=await import(moduleUrl(new URL('../src/order-group-result.ts',import.meta.url)));
const {assessQuery}=await import(moduleUrl(new URL('../src/query-assessment.ts',import.meta.url)));
const args={since:'2026-10-01T00:00:00Z',until:'2026-10-02T00:00:00Z',status:'all',metric:'order_count',group_by:'day',bucket_timezone:'UTC+08:00'};
const text='统计创建时间从“2026-10-01T00:00:00Z”到“2026-10-02T00:00:00Z”的全部订单数量，按日分组，时区UTC+08:00';
test('daily natural language and explicit timezone normalize exactly into the unified candidate',()=>{
 const p=previewOrderGroups(args);assert.deepEqual(proposeOrderGroups(text).request,p.request);
 assert.equal(assessQuery(text).status,'supported');assert.match(p.explanation.join(' '),/32个日历日/);
 assert.deepEqual(previewOrderGroups({...args,since:'2026-10-01T08:00:00+08:00'}).request,p.request);
 for(const metric of ['金额','平均金额'])assert.equal(proposeOrderGroups(text.replace('全部订单数量',`已完成订单${metric}`).replace('按日分组','按日和币种分组')).request.group_by,'day_currency');
});
for(const change of [{bucket_timezone:undefined},{bucket_timezone:'Asia/Shanghai'},{bucket_timezone:'America/New_York'},{bucket_timezone:'UTC+09:00'},{metric:'status_share'},{group_by:'day_currency'},{metric:'completed_amount'},{metric:'completed_average',group_by:'day_currency'},{group_by:'status'}])test(`daily rejects ${JSON.stringify(change)}`,()=>assert.throws(()=>previewOrderGroups({...args,...change})));
for(const bad of [text.replace('，时区UTC+08:00',''),text+'，发送给老板',text.replace('UTC+08:00','Asia/Shanghai'),text.replace('全部订单数量','全部订单金额')])test(`ambiguous or wider question rejects ${bad}`,()=>assert.throws(()=>proposeOrderGroups(bad)));
const output={profile:'synthetic-order-groups-v1',snapshot_guaranteed:false,since:args.since,until:args.until,status_filter:args.status,metric:args.metric,group_by:args.group_by,bucket_timezone:args.bucket_timezone,view_version:'v1',policy_version:'p',read_started:'a',read_finished:'b',complete:true,reason:null,groups:[{key:{day:'2026-10-01'},order_count:1,bucket_since:args.since,bucket_until:'2026-10-01T16:00:00Z',partial_bucket:true},{key:{day:'2026-10-02'},order_count:2,bucket_since:'2026-10-01T16:00:00Z',bucket_until:args.until,partial_bucket:true}]};
const events=[{seq:1,type:'tool.requested',payload:{tool:'analytics.order_groups',call_id:'a',input:args}},{seq:2,type:'tool.result',payload:{call_id:'a',status:'ok',output}}];
test('projection checks exact clipped bucket bounds, timezone echo and no invented empty days',()=>{
 assert.equal(orderGroupResults(events)[0].value.groups.length,2);
 for(const mutate of [e=>e[1].payload.output.bucket_timezone='UTC',e=>e[1].payload.output.groups[0].key.day='2026-02-30',e=>e[1].payload.output.groups[0].key.day='2026-09-30',e=>e[1].payload.output.groups[0].bucket_since='2026-09-30T16:00:00Z',e=>e[1].payload.output.groups[0].bucket_until=args.until,e=>e[1].payload.output.groups[0].partial_bucket=false,e=>e[1].payload.output.groups.push(e[1].payload.output.groups[0]),e=>e[1].payload.output.groups[0].key.currency='CNY',e=>e[1].payload.output.groups[0].order_count=0]){
  const copy=structuredClone(events);mutate(copy);assert.deepEqual(orderGroupResults(copy),[]);
 }
 const empty=structuredClone(events);empty[1].payload.output.groups=[];assert.deepEqual(orderGroupResults(empty)[0].value.groups,[]);
 Object.assign(empty[1].payload.output,{complete:false,reason:'来源截断'});assert.equal(orderGroupResults(empty)[0].value.complete,false);
 empty[1].payload.output.groups=output.groups;assert.deepEqual(orderGroupResults(empty),[]);
});
test('daily money preserves exact per-currency completeness, zero and average fractions',()=>{
 const e=structuredClone(events);Object.assign(e[0].payload.input,{metric:'completed_average',status:'completed',group_by:'day_currency'});
 Object.assign(e[1].payload.output,{metric:'completed_average',status_filter:'completed',group_by:'day_currency'});
 const g=e[1].payload.output.groups[0];e[1].payload.output.groups=[g];Object.assign(g,{key:{day:'2026-10-01',currency:'CNY'},order_count:2,valid_amount_count:1,known_minor:'200',total_minor:null,amount_coverage:0.5,amount_complete:false,average_numerator_minor:null,average_denominator:null});
 assert.equal(orderGroupResults(e)[0].value.groups[0].total_minor,null);
 for(const mutate of [g=>g.total_minor='200',g=>g.average_numerator_minor='200',g=>g.average_denominator=1,g=>g.amount_complete=true,g=>g.key.currency='EUR']){const copy=structuredClone(e);mutate(copy[1].payload.output.groups[0]);assert.deepEqual(orderGroupResults(copy),[]);}
 Object.assign(g,{valid_amount_count:2,known_minor:'0',total_minor:'0',amount_coverage:1,amount_complete:true,average_numerator_minor:'0',average_denominator:2});assert.equal(orderGroupResults(e)[0].value.groups[0].average_denominator,2);
});
