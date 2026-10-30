import {parseOrderPack} from "./order-scenario";
import {parseTicketPack} from "./ticket-scenario";
import {parseSalesPack} from "./sales-scenario";
export function parseScenarioPack(text:string) {
 if(new TextEncoder().encode(text).length>16384)throw new Error("场景包超过16KiB");
 const value=JSON.parse(text);
 if(value?.scenario_id==="order-metrics-readonly")return parseOrderPack(text);
 if(value?.scenario_id==="sales-summary-readonly")return parseSalesPack(text);
 return parseTicketPack(text);
}
