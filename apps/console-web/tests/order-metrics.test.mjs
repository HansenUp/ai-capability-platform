import test from 'node:test';import assert from 'node:assert/strict';import {readFileSync} from 'node:fs';
import {parseOrderPack} from '../src/order-scenario.ts';import {orderMetrics} from '../src/order-metrics.ts';
test('经营包固定profile，不接受权限或真实模型扩展',()=>{
 const raw=readFileSync(new URL('../src/scenarios/order-metrics.json',import.meta.url),'utf8');const pack=parseOrderPack(raw);
 assert.equal(pack.agent.model_alias,'synthetic-analytics');
 for(const mutate of [p=>p.agent.config.tools.push('mail.send'),p=>p.agent.tenant='other',p=>p.agent.harness='real',p=>p.cases[0].expected_text='999']){
 const v=structuredClone(pack);mutate(v);assert.throws(()=>parseOrderPack(JSON.stringify(v)));}
});
test('指标只来自匹配工具回执，未知不补零，畸形与失败不显示成功卡',()=>{
 const output={profile:'synthetic-order-metrics-v1',since:'x',until:'y',view_version:'v1',policy_version:'p1',read_started:'a',read_finished:'b',complete:true,reason:null,order_count:1,statuses:{completed:1,pending:0,cancelled:0},unknown_updated:0,currencies:[{currency:'CNY',completed:1,valid:0,total_minor:null,known_minor:null,coverage:0}]};
 const events=[{seq:1,type:'tool.requested',payload:{tool:'analytics.orders',call_id:'a'}},{seq:2,type:'tool.result',payload:{status:'ok',call_id:'a',output}}];
 assert.equal(orderMetrics(events)[0].value.currencies[0].total_minor,null);
 for(const mutate of [e=>e[1].payload.status='error',e=>e[1].payload.call_id='foreign',e=>e[1].payload.output.order_count=1000,e=>e[1].payload.output.currencies[0].total_minor='0',e=>e[1].payload.output.currencies[0].coverage=NaN]){
 const v=structuredClone(events);mutate(v);assert.deepEqual(orderMetrics(v),[]);}
 assert.deepEqual(orderMetrics([{seq:1,type:'model.response',payload:{output}}]),[]);
});
