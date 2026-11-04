import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {moduleUrl} from './support/compile-query-modules.mjs';
const {assessQuery}=await import(moduleUrl(new URL('../src/query-assessment.ts',import.meta.url)));
const {cases}=JSON.parse(readFileSync(new URL('./fixtures/unified-query-evaluation.json',import.meta.url),'utf8'));
for(const c of cases)test(`unified evaluation ${c.id}`,()=>{
 const r=assessQuery(c.text);assert.equal(r.status,c.status);
 if(c.route){assert.equal(r.route,c.route);assert.ok(r.request);}
 else {assert.equal(r.request,undefined);assert.equal(r.route,undefined);}
 if(c.status==='needs_clarification')assert.ok(r.missing.length);
});
const legacy=JSON.parse(readFileSync(new URL('./fixtures/restricted-query-business-questions.json',import.meta.url),'utf8'));
for(const c of legacy.cases)test(`legacy boundary ${c.id}`,()=>{
 const r=assessQuery(c.text);
 if(c.request){assert.equal(r.status,'supported');assert.deepEqual(r.request,c.request);}
 else assert.notEqual(r.status,'supported');
});
test('oversize question never produces a request',()=>{const r=assessQuery('x'.repeat(513));assert.equal(r.status,'not_supported');assert.equal(r.request,undefined);});
