import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import ts from 'typescript';
const output = ts.transpileModule(readFileSync(new URL('../src/test-token-expiry.ts', import.meta.url), 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022},
}).outputText;
const {testTokenExpiry} = await import(`data:text/javascript;base64,${Buffer.from(output).toString('base64')}`);
const token = payload => `${Buffer.from('{}').toString('base64url')}.${Buffer.from(JSON.stringify(payload)).toString('base64url')}.synthetic`;

test('expiry parsing is UI metadata only and never claims to verify the signature', () => {
  assert.equal(testTokenExpiry(token({exp: 2000000000, sub: '合成用户'})), 2000000000000);
  assert.equal(testTokenExpiry(token({exp: 1})), 1000); // Expired values remain expired; the UI checks its clock.
});

for (const exp of [undefined, null, true, '2000000000', 0, -1, 1.5, Number.MAX_SAFE_INTEGER, Infinity]) {
  test(`malformed expiry rejected: ${String(exp)}`, () => assert.equal(testTokenExpiry(token({exp})), null));
}

for (const value of ['', 'a.b', 'a.b.c.d', '.e30.sig', 'e30..sig', 'e30.e30.', 'e30._w.sig', token([]), token(null)]) {
  test(`malformed token rejected: ${value}`, () => assert.equal(testTokenExpiry(value), null));
}
