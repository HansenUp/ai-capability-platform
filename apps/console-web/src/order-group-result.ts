import {previewOrderGroups, type GroupRequest} from "./order-group-query";
type Event={seq:number;type:string;payload:Record<string,unknown>};
export interface GroupRow {key:{status?:string;currency?:string};order_count:number;valid_amount_count?:number;known_minor?:string|null;total_minor?:string|null;amount_coverage?:number;amount_complete?:boolean}
export interface GroupResult {profile:string;since:string;until:string;status_filter:string;metric:string;group_by:string;view_version:string;policy_version:string;read_started:string;read_finished:string;snapshot_guaranteed:boolean;complete:boolean;reason:string|null;groups:GroupRow[]}
const integer=(x:unknown):x is number=>Number.isInteger(x)&&Number(x)>=0&&Number(x)<=100;
const money=(x:unknown):x is string=>typeof x==="string"&&/^(0|[1-9][0-9]*)$/.test(x)&&x.length<=128;
export function orderGroupResults(events:Event[]):{seq:number;value:GroupResult}[] {
  const calls=new Map(events.filter(e=>e.type==="tool.requested"&&e.payload.tool==="analytics.order_groups").map(e=>[e.payload.call_id,e]));
  return events.flatMap(e=>{
    const call=calls.get(e.payload.call_id),v=e.payload.output as GroupResult|undefined;
    if(!call||call.seq>=e.seq||e.type!=="tool.result"||e.payload.status!=="ok"||!v||v.profile!=="synthetic-order-groups-v1"
      ||v.snapshot_guaranteed!==false||typeof v.complete!=="boolean"||!Array.isArray(v.groups)||v.groups.length>100
      ||![v.view_version,v.policy_version,v.read_started,v.read_finished].every(x=>typeof x==="string"&&!!x))return [];
    try {
      const expected=previewOrderGroups((call.payload.effective_input??call.payload.input) as GroupRequest).request;
      const received=previewOrderGroups({since:v.since,until:v.until,status:v.status_filter,metric:v.metric,group_by:v.group_by}).request;
      if(JSON.stringify(expected)!==JSON.stringify(received))return [];
    } catch{return [];}
    if(!v.complete)return v.groups.length===0&&typeof v.reason==="string"&&!!v.reason?[{seq:e.seq,value:v}]:[];
    if(v.reason!==null)return [];
    const keys=new Set<string>();let total=0;
    for(const g of v.groups){
      if(!g||!g.key||typeof g.key!=="object"||!integer(g.order_count)||g.order_count===0)return [];
      const fields=Object.keys(g.key).sort().join();
      if(fields!==(v.group_by==="status_currency"?"currency,status":v.group_by))return [];
      if(g.key.status!==undefined&&(!["pending","completed","cancelled"].includes(g.key.status)||(v.status_filter!=="all"&&g.key.status!==v.status_filter)))return [];
      if(g.key.currency!==undefined&&!["CNY","USD"].includes(g.key.currency))return [];
      const key=JSON.stringify([g.key.status??null,g.key.currency??null]);if(keys.has(key))return [];keys.add(key);
      total+=g.order_count;if(total>100)return [];
      if(v.metric==="completed_amount"){
        const n=g.valid_amount_count;
        if(!integer(n)||n>g.order_count||g.amount_complete!==(n===g.order_count)||g.amount_coverage!==n/g.order_count
          ||(n===0?g.known_minor!==null:!money(g.known_minor))
          ||(n===g.order_count?g.total_minor!==g.known_minor:g.total_minor!==null))return [];
      }else if(Object.keys(g).sort().join()!=="key,order_count")return [];
    }
    return [{seq:e.seq,value:v}];
  });
}
