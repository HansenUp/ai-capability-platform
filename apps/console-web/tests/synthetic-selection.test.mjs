import test from "node:test";
import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import {previewSelection, catalogRevision} from "../src/synthetic-selection.ts";
const catalogs = await Promise.all(["ticket-readonly", "sales-readonly", "order-metrics"].map(async name => JSON.parse(await readFile(new URL(`../src/scenarios/${name}.json`, import.meta.url), "utf8"))));
const selection = c => ({schema_version:1,scenario_id:c.scenario_id,catalog_revision:catalogRevision,case_ids:c.cases.map(row=>row.id)});
test("fixed catalog selections include all eight cases without changing expectations", () => {
 assert.equal(catalogs.reduce((n,c)=>n+c.cases.length,0),8);
 for(const c of catalogs) assert.deepEqual(previewSelection(JSON.stringify(selection(c)),catalogs).case_ids,c.cases.map(row=>row.id));
});
test("rejects substituted expectations, authority, unknown and duplicate references", () => {
 for(const change of [v=>v.tenant="other",v=>v.agent_id="other",v=>v.expected_text="pass",v=>v.schema_version=true,v=>v.catalog_revision="next",v=>v.scenario_id="unknown",v=>v.case_ids=[],v=>v.case_ids=[true],v=>v.case_ids=["unknown"],v=>v.case_ids=["success","success"]]) {
  const v=selection(catalogs[0]);change(v);assert.throws(()=>previewSelection(JSON.stringify(v),catalogs));
 }
 for(const text of ["null","[]","{",'{"schema_version":1,"schema_version":1}', '{"case_ids":[],"case_\\u0069ds":[]}']) assert.throws(()=>previewSelection(text,catalogs));
});
test("enforces UTF8 byte bound including whitespace",()=>{
 const text=JSON.stringify(selection(catalogs[0]));
 assert.doesNotThrow(()=>previewSelection(text+" ".repeat(16384-Buffer.byteLength(text)),catalogs));
 assert.throws(()=>previewSelection(text+" ".repeat(16385-Buffer.byteLength(text)),catalogs));
 assert.throws(()=>previewSelection(JSON.stringify({...selection(catalogs[0]),extra:"中".repeat(6000)}),catalogs),/16KiB/);
});
