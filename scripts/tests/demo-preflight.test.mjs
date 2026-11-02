import test from "node:test";
import assert from "node:assert/strict";
import {mkdtemp, mkdir, writeFile, rm, readdir} from "node:fs/promises";
import {join} from "node:path";
import {tmpdir} from "node:os";
import {preflight} from "../demo-preflight.mjs";

async function fixture(run) {
 const root=await mkdtemp(join(tmpdir(),"demo-preflight-"));
 try {
  for(const name of [".venv/bin/python","scripts/verify_s12_workbench.py","scripts/local_demo.html","scripts/local_demo_ticket_readonly.html","apps/portal-web/demo.html","modules/vite.mjs","modules/vue.mjs","modules/playwright.mjs"]){
   const path=join(root,name);await mkdir(join(path,".."),{recursive:true});await writeFile(path,"synthetic static placeholder",{mode:0o700});
  }
  const options={root,args:[],env:{},resolveModule:name=>join(root,"modules",name==="@vitejs/plugin-vue"?"vue.mjs":`${name}.mjs`)};
  await run(options);
 } finally {await rm(root,{recursive:true});}
}
test("preflight resolves local entries without executing or creating evidence",()=>fixture(async o=>{
 o.env.S12_DEMO_EVIDENCE=join(o.root,"new-evidence","nested");
 assert.equal((await preflight(o)).python,join(o.root,".venv/bin/python"));
 assert.ok(!(await readdir(o.root)).includes("new-evidence"));
}));
test("missing Python and dependency have actionable errors",()=>fixture(async o=>{
 await rm(join(o.root,".venv/bin/python"));await assert.rejects(preflight(o),/DEMO_CONFIG.*Python/);
}));
test("missing Vite does not import or install",()=>fixture(async o=>{
 o.resolveModule=()=>{throw new Error("missing");};await assert.rejects(preflight(o),/DEMO_CONFIG.*vite/);
}));
test("invalid evidence target is refused before environment creation",()=>fixture(async o=>{
 o.env.S12_DEMO_EVIDENCE=join(o.root,"modules/vite.mjs");await assert.rejects(preflight(o),/S12_DEMO_EVIDENCE/);
}));
test("unknown and conflicting modes refuse silent fallback",()=>fixture(async o=>{
 for(const args of [["--typo"],["--f6-readonly","--b3-read"]]) await assert.rejects(preflight({...o,args}),/DEMO_CONFIG/);
}));
test("missing browser entry is detected before service startup",()=>fixture(async o=>{
 o.env.PLAYWRIGHT_MODULE=join(o.root,"missing.mjs");await assert.rejects(preflight(o),/PLAYWRIGHT_MODULE/);
}));
