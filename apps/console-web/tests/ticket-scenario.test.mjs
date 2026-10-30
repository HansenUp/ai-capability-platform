import test from "node:test";
import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import {parseTicketPack,evaluateTicketEvents} from "../src/ticket-scenario.ts";
const raw=await readFile(new URL("../src/scenarios/ticket-readonly.json",import.meta.url),"utf8");
test("scenario import pins isolation, permissions and evaluation expectations",()=>{
 const pack=parseTicketPack(raw);assert.equal(pack.agent.harness,"mock");
 for(const mutate of [p=>p.schema_version=2,p=>p.agent.harness="real",p=>p.agent.config.tools.push("ticket.close"),p=>p.agent.config.endpoint="https://example.invalid",p=>p.tenant="other",p=>p.cases[1].expected_text="fake pass",p=>p.cases=[]]){
  const changed=JSON.parse(raw);mutate(changed);assert.throws(()=>parseTicketPack(JSON.stringify(changed)));
 }
 assert.throws(()=>parseTicketPack("{"));assert.throws(()=>parseTicketPack(" ".repeat(16385)));
});
test("synthetic event evaluation rejects false success, missing evidence and write tools",()=>{
 const pack=parseTicketPack(raw);
 for(const row of pack.cases){
  const events=[{type:"run.created"},{type:"tool.requested",payload:{tool:"ticket.get"}},{type:"tool.result",payload:{status:row.tool_status}},{type:"model.response",payload:{text:row.expected_text}}];
  assert.deepEqual(evaluateTicketEvents(pack,row.id,events),{passed:true,reasons:[]});
  assert.equal(evaluateTicketEvents(pack,row.id,events.slice(1)).passed,false);
  const other=structuredClone(events);other[1].payload.tool="ticket.close";assert.equal(evaluateTicketEvents(pack,row.id,other).passed,false);
  if(row.id==="failure")assert.equal(evaluateTicketEvents(pack,row.id,[...events,{type:"model.response",payload:{text:pack.cases[0].expected_text}}]).passed,false);
 }
});
