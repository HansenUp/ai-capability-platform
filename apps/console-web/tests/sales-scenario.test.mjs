import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {parseSalesPack} from "../src/sales-scenario.ts";
const text=readFileSync(new URL("../src/scenarios/sales-readonly.json",import.meta.url),"utf8");
test("sales exact profile preserves immutable cases and refuses widening",()=>{
 const pack=parseSalesPack(text);assert.equal(pack.cases.length,3);
 for(const mutate of [
 p=>p.agent.config.tools.push("mail.send"),
 p=>p.agent.config.tenant="foreign",
 p=>p.agent.harness="real",
 p=>p.cases[2].expected_text="success",
 p=>p.agent.token="secret",
 p=>p.scenario_id="ticket-summary-readonly",
 ]){const invalid=structuredClone(pack);mutate(invalid);assert.throws(()=>parseSalesPack(JSON.stringify(invalid)));}
});
