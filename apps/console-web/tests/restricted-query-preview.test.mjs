import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import ts from 'typescript';
const output = ts.transpileModule(readFileSync(new URL('../src/restricted-query-preview.ts', import.meta.url), 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022},
}).outputText;
const {previewRestrictedQuery} = await import(`data:text/javascript;base64,${Buffer.from(output).toString('base64')}`);
const draft = changes => ({version: 'v1', columns: ['item'], filter: '', limit: 10, ...changes});

test('preview preserves F4 structured request and keeps filter text out of SQL', () => {
  const filter = "a1' OR 1=1 -- <script>";
  const result = previewRestrictedQuery(draft({columns: ['secret', 'item'], filter, limit: 100}));
  assert.deepEqual(result.request, {view_id: 'orders', version: 'v1', columns: ['secret', 'item'], filters: [{column: 'item', value: filter}], limit: 100});
  assert.equal(result.template.includes(filter), false);
  assert.match(result.template, /:mask AS "secret"/);
  assert.match(result.template, /COLLATE BINARY = :tenant/);
  assert.deepEqual(result.bindings, [
    {name: ':mask', source: '固定遮盖值', value: '[REDACTED]'},
    {name: ':tenant', source: '执行时从已验证身份取得，不接受浏览器输入'},
    {name: ':item', source: 'item 等值条件，作为参数绑定', value: filter},
    {name: ':fetch_limit', source: '多取一行以判断截断', value: 101},
  ]);
});

test('no filter still requires server tenant and explains preview limitations', () => {
  const result = previewRestrictedQuery(draft({limit: 1}));
  assert.deepEqual(result.request.filters, []);
  assert.equal(result.bindings.some(binding => binding.name === ':item'), false);
  assert.equal(result.bindings.find(binding => binding.name === ':tenant').value, undefined);
  assert.equal(result.bindings.at(-1).value, 2);
  assert.match(result.explanation.join(' '), /不创建 Run/);
  assert.match(result.explanation.join(' '), /不保证排序/);
});

test('preview snapshots mutable form arrays', () => {
  const input = draft(); const result = previewRestrictedQuery(input);
  input.columns.push('secret'); input.filter = 'changed'; input.limit = 2;
  assert.deepEqual(result.request.columns, ['item']);
  assert.equal(result.request.limit, 10);
  assert.deepEqual(result.request.filters, []);
});

for (const changes of [
  {version: ''}, {version: 'v1;DROP'}, {version: 'v'.repeat(65)},
  {columns: []}, {columns: ['tenant']}, {columns: ['item', 'item']},
  {columns: ['*']}, {limit: 0}, {limit: 101}, {limit: 1.5}, {limit: NaN}, {filter: 'x'.repeat(129)},
]) test(`out-of-scope draft rejected: ${JSON.stringify(changes)}`, () => {
  assert.throws(() => previewRestrictedQuery(draft(changes)));
});
