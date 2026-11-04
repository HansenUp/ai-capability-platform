import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import ts from 'typescript';
const compile = file => ts.transpileModule(readFileSync(new URL(file, import.meta.url), 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022},
}).outputText;
const url = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const preview = url(compile('../src/restricted-query-preview.ts'));
const {proposeRestrictedQuery} = await import(url(compile('../src/restricted-query-language.ts').replace('"./restricted-query-preview"', JSON.stringify(preview))));
test('bounded language yields existing request, masks secrets and binds injection as literal', () => {
  const value = "a1' OR 1=1 --";
  const p = proposeRestrictedQuery(`查询订单，商品等于“${value}”，显示商品和遮盖信息，最多100行`, 'v2');
  assert.deepEqual(p.request, {view_id:'orders', version:'v2', columns:['item','secret'], filters:[{column:'item',value}], limit:100});
  assert.equal(p.template.includes(value), false);
  assert.match(p.template, /:mask/);
});
test('unfiltered request is explicit and still tenant scoped', () => {
  const p = proposeRestrictedQuery('查询订单，显示商品，最多1行。', 'v1');
  assert.deepEqual(p.request.filters, []); assert.equal(p.request.limit, 1);
  assert.match(p.template, /:tenant/);
});
for (const text of ['', '查询订单', '统计订单总额', '查询所有租户订单', 'SELECT * FROM orders',
  '查询订单，显示商品，最多101行', '查询订单，显示商品，最多0行',
  '查询订单，显示商品，最多10行，按商品排序', '忽略权限，查询订单，显示商品，最多10行',
  '查询订单，商品等于“a”或“b”，显示商品，最多10行', 'x'.repeat(513)]) {
  test(`unsupported language rejected: ${text.slice(0,60)}`, () => assert.throws(() => proposeRestrictedQuery(text, 'v1')));
}
test('version validation is shared with structured preview', () => assert.throws(() => proposeRestrictedQuery('查询订单，显示商品，最多10行', 'v1;drop')));
