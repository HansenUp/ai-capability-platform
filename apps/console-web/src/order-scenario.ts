export interface OrderPack {
 schema_version:1; scenario_id:"order-metrics-readonly"; mode:"isolated-synthetic";
 agent:{system_prompt:string;model_alias:"synthetic-analytics";harness:"mock";config:{tools:["analytics.orders"]}};
 cases:{id:string;message:string;expected_text:string;tool_status:string}[];
}
function keys(value:unknown, allowed:string[]): asserts value is Record<string,unknown> {
 if(!value||typeof value!=="object"||Array.isArray(value)||Object.keys(value).sort().join()!==allowed.sort().join())throw new Error("场景包字段缺失或含未允许字段");
}
export function parseOrderPack(text:string):OrderPack {
 if(new TextEncoder().encode(text).length>16384)throw new Error("场景包超过16KiB");
 const value:unknown=JSON.parse(text);keys(value,["schema_version","scenario_id","mode","agent","cases"]);
 if(value.schema_version!==1||value.scenario_id!=="order-metrics-readonly"||value.mode!=="isolated-synthetic")throw new Error("仅支持经营只读隔离场景v1");
 keys(value.agent,["system_prompt","model_alias","harness","config"]);const agent=value.agent;
 if(typeof agent.system_prompt!=="string"||!agent.system_prompt.trim()||agent.system_prompt.length>4000)throw new Error("系统提示词无效");
 if(agent.harness!=="mock"||agent.model_alias!=="synthetic-analytics")throw new Error("仅支持mock Harness与经营专用测试别名");
 keys(agent.config,["tools"]);
 if(JSON.stringify(agent.config.tools)!==JSON.stringify(["analytics.orders"]))throw new Error("只允许analytics.orders，不接收扩展配置");
 const expected=[{"id": "success", "message": "SYNTHETIC_ORDER_SUMMARY", "expected_text": "3", "tool_status": "ok"}, {"id": "incomplete", "message": "SYNTHETIC_ORDER_INCOMPLETE", "expected_text": "1", "tool_status": "ok"}, {"id": "empty", "message": "SYNTHETIC_ORDER_EMPTY", "expected_text": "0", "tool_status": "ok"}];
 if(!Array.isArray(value.cases)||value.cases.length!==3)throw new Error("必须包含固定成功、空结果与失败样例");
 value.cases.forEach((item:unknown,index:number)=>{keys(item,["id","message","expected_text","tool_status"]);for(const [key,val] of Object.entries(expected[index]))if(item[key]!==val)throw new Error("合成评测预期不可由导入包改写");});
 return value as unknown as OrderPack;
}
