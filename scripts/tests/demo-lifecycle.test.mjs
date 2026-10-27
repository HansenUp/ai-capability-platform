import test from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdtemp, readdir, readFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {once} from 'node:events';

for (const mode of ['verify', 'interrupt', 'browser-start-failure']) {
 test(`isolated demo lifecycle: ${mode}`, async () => {
  const root = await mkdtemp(join(tmpdir(),'demo-lifecycle-'));
  let output = '', sent = false;
  const child = spawn(process.execPath, ['scripts/start_local_demo.mjs', ...(mode==='verify'?['--verify-only']:[])], {
   env: {PATH:process.env.PATH,HOME:process.env.HOME,TMPDIR:root,
    PLAYWRIGHT_MODULE:mode==='browser-start-failure'?'file:///synthetic-missing-playwright.mjs':process.env.PLAYWRIGHT_MODULE,
    S12_DEMO_EVIDENCE:join(root,'evidence')},stdio:['ignore','pipe','pipe']
  });
  const collect = data => {output+=data; if(mode==='interrupt' && output.includes('DEMO_READY') && !sent){sent=true;child.kill('SIGINT');setTimeout(()=>{if(child.exitCode===null && child.signalCode===null)child.kill('SIGINT');},100);}};
  child.stdout.on('data',collect);child.stderr.on('data',collect);
  const timeout = setTimeout(()=>child.kill('SIGTERM'),90000);
  try {
   const [code,signal]=await once(child,'exit');
   assert.equal(signal,null,output);
   assert.equal(code,mode==='browser-start-failure'?1:0,output);
   assert.ok(output.includes('CLEANUP: owned demo temporary root removed'),output);
   assert.deepEqual((await readdir(root)).filter(name=>name.startsWith('ai-platform-demo-')),[]);
   const log=await readFile(join(root,'evidence','fixture.log'),'utf8');
   assert.ok(!log.includes('KeyboardInterrupt'),log);
   console.log(`${mode}: owned temporary roots removed, fixture log retained during test, exit=${code}`);
  } finally {clearTimeout(timeout); if(child.exitCode!==null || child.signalCode!==null)await rm(root,{recursive:true});}
 });
}
