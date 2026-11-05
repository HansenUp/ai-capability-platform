/** Local candidates only. Fixed mappings do not grant permission or call Runtime. */
import {proposeRestrictedQuery} from "../../console-web/src/restricted-query-language";
import {previewOrderCount, proposeOrderCount} from "../../console-web/src/order-count-query";
import {previewOrderGroups, proposeOrderGroups} from "../../console-web/src/order-group-query";
import {proposeOrderCompare, compareCapability} from "../../console-web/src/order-compare-query";
import {assessQuery, queryCapabilities} from "../../console-web/src/query-assessment";
export const portalCapabilities = [...queryCapabilities, compareCapability];
export interface PortalCandidate { agent: string; text: string; request: unknown; explanation: string[] }
function candidate(kind: "detail"|"count"|"groups"|"compare", plan: {request:unknown;explanation:string[]}): PortalCandidate {
  const fixed = {detail:["f4-data-query","data.query"],count:["f10-order-count","analytics.order_count"],groups:["f10-order-groups","analytics.order_groups"],compare:["f10-order-compare","analytics.order_compare"]} as const;
  const [agent,tool]=fixed[kind];
  return {agent,text:`!tool ${tool} ${JSON.stringify(plan.request)}`,request:plan.request,explanation:plan.explanation};
}
export function proposePortalQuery(text:string,version="v1"):PortalCandidate {
  for (const [kind,parse] of [
    ["detail",()=>proposeRestrictedQuery(text,version)],
    ["count",()=>proposeOrderCount(text)],
    ["groups",()=>proposeOrderGroups(text)],
    ["compare",()=>proposeOrderCompare(text)],
  ] as const) { try { return candidate(kind,parse()); } catch { /* Full parsers must accept before a candidate exists. */ } }
  throw new Error(assessQuery(text,version).reason);
}
export function previewPortalQuery(input:{kind:string;since:string;until:string;status:string;metric:string;group_by:string;bucket_timezone:string}):PortalCandidate {
  if(input.kind==="count")return candidate("count",previewOrderCount(input));
  if(input.kind!=="groups")throw new Error("请选择订单数量或受限分组。");
  const {since,until,status,metric,group_by,bucket_timezone}=input;
  return candidate("groups",previewOrderGroups({since,until,status,metric,group_by,...(group_by.startsWith("day")?{bucket_timezone}:{})}));
}
