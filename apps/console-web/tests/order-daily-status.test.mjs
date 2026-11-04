import test from 'node:test';
import assert from 'node:assert/strict';
import {moduleUrl} from './support/compile-query-modules.mjs';
const {previewOrderGroups,proposeOrderGroups}=await import(moduleUrl(new URL('../src/order-group-query.ts',import.meta.url)));
const {orderGroupResults}=await import(moduleUrl(new URL('../src/order-group-result.ts',import.meta.url)));
const {assessQuery}=await import(moduleUrl(new URL('../src/query-assessment.ts',import.meta.url)));
const args={since:'2026-10-01T00:00:00Z',until:'2026-10-02T00:00:00Z',status:'all',metric:'order_count',group_by:'day_status',bucket_timezone:'UTC+08:00'};
const text='统计创建时间从“2026-10-01T00:00:00Z”到“2026-10-02T00:00:00Z”的全部订单数量，按日和状态分组，时区UTC+08:00';
test('daily/status explicit natural language, single statuses and unified assessment retain exact scope',()=>{
 const p=previewOrderGroups(args);assert.deepEqual(proposeOrderGroups(text).request,p.request);assert.equal(assessQuery(text).status,'supported');assert.match(p.explanation.join(' '),/不补空日期或零行/);
 for(const [name,status] of [['待完成','pending'],['已完成','completed'],['已取消','cancelled']])assert.equal(proposeOrderGroups(text.replace('全部',name)).request.status,status);
 assert.equal(proposeOrderGroups(text.replace('UTC+08:00','UTC')).request.bucket_timezone,'UTC');
});
for(const change of [{metric:'completed_amount',status:'completed'},{metric:'completed_average',status:'completed'},{metric:'status_share'},{bucket_timezone:undefined},{bucket_timezone:'Asia/Shanghai'},{group_by:'day_status_currency'}])test(`daily/status rejects wider scope ${JSON.stringify(change)}`,()=>assert.throws(()=>previewOrderGroups({...args,...change})));
for(const bad of [text.replace('，时区UTC+08:00',''),text.replace('全部订单数量','已完成订单金额'),text.replace('全部订单数量','全部订单状态占比'),text+'，然后发送',text.replace('日和状态','日和状态和币种')])test(`daily/status question rejects ${bad}`,()=>assert.throws(()=>proposeOrderGroups(bad)));
const group=(day,status,n)=>({key:{day,status},order_count:n,bucket_since:day==='2026-10-01'?args.since:'2026-10-01T16:00:00Z',bucket_until:day==='2026-10-01'?'2026-10-01T16:00:00Z':args.until,partial_bucket:true});
const output={profile:'synthetic-order-groups-v1',snapshot_guaranteed:false,since:args.since,until:args.until,status_filter:'all',metric:'order_count',group_by:'day_status',bucket_timezone:args.bucket_timezone,view_version:'v1',policy_version:'p',read_started:'a',read_finished:'b',complete:true,reason:null,groups:[group('2026-10-01','completed',3),group('2026-10-01','pending',1),group('2026-10-02','completed',1),group('2026-10-02','pending',1)]};
const events=[{seq:1,type:'tool.requested',payload:{tool:'analytics.order_groups',call_id:'a',input:args}},{seq:2,type:'tool.result',payload:{call_id:'a',status:'ok',output}}];
test('daily/status projection distinguishes same date across statuses and same status across dates',()=>{
 assert.equal(orderGroupResults(events)[0].value.groups.length,4);
 const copy=structuredClone(events);copy[1].payload.output.groups=[];assert.deepEqual(orderGroupResults(copy)[0].value.groups,[]);
 Object.assign(copy[1].payload.output,{complete:false,reason:'来源截断'});assert.equal(orderGroupResults(copy)[0].value.complete,false);
 copy[1].payload.output.groups=output.groups;assert.deepEqual(orderGroupResults(copy),[]);
});
test('daily/status projection rejects forged keys, zero-fill, wrong bounds and metric fields',()=>{
 for(const mutate of [g=>delete g.key.status,g=>g.key.status='paid',g=>g.key.currency='CNY',g=>g.order_count=0,g=>g.key.day='2026-10-03',g=>g.bucket_since='2026-09-30T16:00:00Z',g=>g.partial_bucket=false,g=>g.total_minor='100',g=>g.share_numerator=1]){
  const copy=structuredClone(events);mutate(copy[1].payload.output.groups[0]);assert.deepEqual(orderGroupResults(copy),[]);
 }
 const dup=structuredClone(events);dup[1].payload.output.groups.push(dup[1].payload.output.groups[0]);assert.deepEqual(orderGroupResults(dup),[]);
 const filtered=structuredClone(events);filtered[0].payload.input.status='completed';filtered[1].payload.output.status_filter='completed';assert.deepEqual(orderGroupResults(filtered),[]);
 filtered[1].payload.output.groups=filtered[1].payload.output.groups.filter(g=>g.key.status==='completed');assert.equal(orderGroupResults(filtered)[0].value.groups.length,2);
 const zone=structuredClone(events);zone[1].payload.output.bucket_timezone='UTC';assert.deepEqual(orderGroupResults(zone),[]);
});
