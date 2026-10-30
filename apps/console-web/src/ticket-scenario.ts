export interface TicketPack {
 schema_version:1; scenario_id:"ticket-summary-readonly"; mode:"isolated-synthetic";
 agent:{system_prompt:string;model_alias:"default-chat";harness:"mock";config:{tools:["ticket.get"]}};
 cases:{id:string;message:string;expected_text:string;tool_status:string}[];
}
function keys(value:unknown, allowed:string[]): asserts value is Record<string,unknown> {
 if(!value||typeof value!=="object"||Array.isArray(value)||Object.keys(value).sort().join()!==allowed.sort().join())throw new Error("场景包字段缺失或含未允许字段");
}
export function parseTicketPack(text:string):TicketPack {
 if(new TextEncoder().encode(text).length>16384)throw new Error("场景包超过16KiB");
 const value:unknown=JSON.parse(text);keys(value,["schema_version","scenario_id","mode","agent","cases"]);
 if(value.schema_version!==1||value.scenario_id!=="ticket-summary-readonly"||value.mode!=="isolated-synthetic")throw new Error("仅支持工单只读隔离场景v1");
 keys(value.agent,["system_prompt","model_alias","harness","config"]);const agent=value.agent;
 if(typeof agent.system_prompt!=="string"||!agent.system_prompt.trim()||agent.system_prompt.length>4000)throw new Error("系统提示词无效");
 if(agent.harness!=="mock"||agent.model_alias!=="default-chat")throw new Error("仅支持mock Harness与默认测试别名");
 keys(agent.config,["tools"]);
 if(JSON.stringify(agent.config.tools)!==JSON.stringify(["ticket.get"]))throw new Error("只允许ticket.get，不接收扩展配置");
 const expected=[
 {id:"success",message:"SYNTHETIC_TICKET_SUMMARY",expected_text:"模拟工单摘要：SYNTHETIC-1 / 设备无法连接 / open",tool_status:"ok"},
 {id:"failure",message:"SYNTHETIC_TICKET_FAILURE",expected_text:"模拟工单查询不可用，未生成摘要。",tool_status:"error"}];
 if(!Array.isArray(value.cases)||value.cases.length!==2)throw new Error("必须包含固定成功与失败合成样例");
 value.cases.forEach((item:unknown,index:number)=>{keys(item,["id","message","expected_text","tool_status"]);for(const [key,val] of Object.entries(expected[index]))if(item[key]!==val)throw new Error("合成评测预期不可由导入包改写");});
 return value as unknown as TicketPack;
}
interface Event {type:string;payload?:Record<string,unknown>}
export function evaluateTicketEvents(pack:TicketPack,caseId:string,events:Event[]):{passed:boolean;reasons:string[]} {
 // Revalidate even programmatic input; imported expectations cannot redefine success.
 const checked=parseTicketPack(JSON.stringify(pack));const sample=checked.cases.find(row=>row.id===caseId);
 if(!sample)throw new Error("未知合成样例");
 const reasons:string[]=[];
 if(events.filter(e=>e.type==="run.created").length!==1)reasons.push("必须恰好创建一个Run");
 const requests=events.filter(e=>e.type==="tool.requested");
 if(requests.length!==1||requests[0].payload?.tool!=="ticket.get")reasons.push("工具调用不符合只读白名单");
 const results=events.filter(e=>e.type==="tool.result");
 if(results.length!==1||results[0].payload?.status!==sample.tool_status)reasons.push("工具结果与样例不符");
 const answers=events.filter(e=>e.type==="model.response"&&typeof e.payload?.text==="string").map(e=>e.payload!.text as string);
 if(!answers.includes(sample.expected_text))reasons.push("缺少合成预期回答");
 if(caseId==="failure"&&answers.some(t=>t.includes("模拟工单摘要：")))reasons.push("工具失败仍生成成功摘要");
 if(events.some(e=>["tool.approved","input.provided"].includes(e.type)))reasons.push("合成只读样例出现额外审批或输入事件");
 return {passed:reasons.length===0,reasons};
}
