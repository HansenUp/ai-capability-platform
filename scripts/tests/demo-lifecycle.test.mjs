import test from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdtemp, readdir, readFile, writeFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {once} from 'node:events';

for (const mode of ['verify', 'interrupt', 'startup-interrupt', 'browser-start-failure', 'missing-playwright', 'preflight-only']) {
 test(`isolated demo lifecycle: ${mode}`, async () => {
  const root = await mkdtemp(join(tmpdir(),'demo-lifecycle-'));
  let output = '', sent = false;
  const broken = join(root, 'broken-browser.mjs');
  if (mode === 'browser-start-failure') await writeFile(broken, 'throw new Error("synthetic browser initialization failure");');
  const child = spawn(process.execPath, ['scripts/start_local_demo.mjs', ...(mode==='verify'?['--verify-only']:mode==='preflight-only'?['--preflight-only']:[])], {
   env: {PATH:process.env.PATH,HOME:process.env.HOME,TMPDIR:root,
    PLAYWRIGHT_MODULE:mode==='browser-start-failure'?broken:mode==='missing-playwright'?'file:///synthetic-missing-playwright.mjs':process.env.PLAYWRIGHT_MODULE,
    S12_DEMO_EVIDENCE:join(root,'evidence')},stdio:['ignore','pipe','pipe']
  });
  const collect = data => {output+=data; if(((mode==='interrupt' && output.includes('DEMO_READY')) || (mode==='startup-interrupt' && output.includes('DEMO_PHASE: fixture-ready'))) && !sent){sent=true;child.kill('SIGINT');setTimeout(()=>{if(child.exitCode===null && child.signalCode===null)child.kill('SIGINT');},100);}};
  child.stdout.on('data',collect);child.stderr.on('data',collect);
  const timeout = setTimeout(()=>child.kill('SIGTERM'),90000);
  try {
   const [code,signal]=await once(child,'exit');
   assert.equal(signal,null,output);
   assert.equal(code,['browser-start-failure','missing-playwright','startup-interrupt'].includes(mode)?1:0,output);
   if (!['missing-playwright','preflight-only'].includes(mode)) assert.ok(output.includes('CLEANUP: owned demo temporary root removed'),output);
   if (mode === 'missing-playwright') assert.match(output,/DEMO_CONFIG.*PLAYWRIGHT_MODULE/);
   if (['missing-playwright','preflight-only','startup-interrupt'].includes(mode)) {
    assert.ok(!output.includes('DEMO_READY'),output);assert.ok(!output.includes('DEMO_PHASE: browser-validation'),output);
   }
   assert.deepEqual((await readdir(root)).filter(name=>name.startsWith('ai-platform-demo-')),[]);
   if (!['missing-playwright','preflight-only'].includes(mode)) {
    const log=await readFile(join(root,'evidence','fixture.log'),'utf8');
    assert.ok(!log.includes('KeyboardInterrupt'),log);
   } else assert.ok(!(await readdir(root)).includes('evidence'),output);
   console.log(`${mode}: owned temporary roots removed, fixture log retained during test, exit=${code}`);
  } finally {clearTimeout(timeout); if(child.exitCode!==null || child.signalCode!==null)await rm(root,{recursive:true});}
 });
}
