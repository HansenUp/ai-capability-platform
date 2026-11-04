import {readFileSync} from 'node:fs';
import ts from 'typescript';
const cache=new Map();
// Repository-owned test modules only; resolves existing relative TS imports.
export function moduleUrl(path) {
  const key=path.href;if(cache.has(key))return cache.get(key);
  let source=ts.transpileModule(readFileSync(path,'utf8'),{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022}}).outputText;
  source=source.replace(/from "(\.\/[^"]+)"/g,(_all,relative)=>`from ${JSON.stringify(moduleUrl(new URL(relative+'.ts',path)))}`);
  const url=`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;cache.set(key,url);return url;
}
