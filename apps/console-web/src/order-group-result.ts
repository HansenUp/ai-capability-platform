import {dailyBounds, type BucketTimezone} from "./order-daily-query";
import {previewOrderGroups, type GroupRequest} from "./order-group-query";
type Event={seq:number;type:string;payload:Record<string,unknown>};
export interface GroupRow {key:{status?:string;currency?:string;day?:string};order_count:number;valid_amount_count?:number;known_minor?:string|null;total_minor?:string|null;amount_coverage?:number;amount_complete?:boolean;average_numerator_minor?:string|null;average_denominator?:number|null;share_numerator?:number|null;share_denominator?:number|null;bucket_since?:string;bucket_until?:string;partial_bucket?:boolean}
export interface GroupResult {profile:string;since:string;until:string;status_filter:string;metric:string;group_by:string;view_version:string;policy_version:string;read_started:string;read_finished:string;snapshot_guaranteed:boolean;complete:boolean;reason:string|null;total_order_count?:number|null;bucket_timezone?:BucketTimezone;groups:GroupRow[]}
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
      const received=previewOrderGroups({since:v.since,until:v.until,status:v.status_filter,metric:v.metric,group_by:v.group_by,bucket_timezone:v.bucket_timezone}).request;
      if(JSON.stringify(expected)!==JSON.stringify(received))return [];
    } catch{return [];}
    if(!v.complete)return (v.metric!=="status_share"||v.total_order_count===null)&&v.groups.length===0&&typeof v.reason==="string"&&!!v.reason?[{seq:e.seq,value:v}]:[];
    if(v.reason!==null)return [];
    const dailyShare=v.metric==="status_share"&&v.group_by==="day_status";
    if(v.metric==="status_share"&&(!integer(v.total_order_count)||(!dailyShare&&v.groups.length!==3)))return [];
    const days=new Map<string,{count:number;denominator:number;statuses:Set<string>}>();
    const daily=v.group_by==="day"||v.group_by==="day_currency"||v.group_by==="day_status";
    const keys=new Set<string>();let total=0;
    for(const g of v.groups){
      if(!g||!g.key||typeof g.key!=="object"||!integer(g.order_count)||(g.order_count===0&&v.metric!=="status_share"))return [];
      const fields=Object.keys(g.key).sort().join();
      if(fields!==(v.group_by==="status_currency"?"currency,status":v.group_by==="day_currency"?"currency,day":v.group_by==="day_status"?"day,status":v.group_by))return [];
      if(g.key.status!==undefined&&(!["pending","completed","cancelled"].includes(g.key.status)||(v.status_filter!=="all"&&g.key.status!==v.status_filter)))return [];
      if(g.key.currency!==undefined&&!["CNY","USD"].includes(g.key.currency))return [];
      if(daily){
        try{const bounds=dailyBounds(g.key.day!,v.since,v.until,v.bucket_timezone!);
          if(new Date(g.bucket_since!).toISOString()!==bounds.bucket_since||new Date(g.bucket_until!).toISOString()!==bounds.bucket_until||g.partial_bucket!==bounds.partial_bucket)return [];
        }catch{return [];}
      }
      const key=JSON.stringify([g.key.day??null,g.key.status??null,g.key.currency??null]);if(keys.has(key))return [];keys.add(key);
      total+=g.order_count;if(total>100)return [];
      if(v.metric==="status_share"){
        if(Object.keys(g).sort().join()!==(dailyShare?"bucket_since,bucket_until,key,order_count,partial_bucket,share_denominator,share_numerator":"key,order_count,share_denominator,share_numerator")
          ||(!dailyShare&&v.total_order_count===0?g.share_numerator!==null||g.share_denominator!==null
            :g.share_numerator!==g.order_count||(dailyShare?!integer(g.share_denominator)||g.share_denominator===0:g.share_denominator!==v.total_order_count)))return [];
        if(dailyShare){const day=g.key.day!,d=days.get(day)??{count:0,denominator:g.share_denominator!,statuses:new Set<string>()};
          if(d.denominator!==g.share_denominator)return [];d.count+=g.order_count;d.statuses.add(g.key.status!);days.set(day,d);
        }
      }else if(v.metric!=="order_count"){
        const n=g.valid_amount_count;
        if(!integer(n)||n>g.order_count||g.amount_complete!==(n===g.order_count)||g.amount_coverage!==n/g.order_count
          ||(n===0?g.known_minor!==null:!money(g.known_minor))
          ||(n===g.order_count?g.total_minor!==g.known_minor:g.total_minor!==null))return [];
        if(v.metric==="completed_average"&&(g.amount_complete
          ?g.average_numerator_minor!==g.total_minor||g.average_denominator!==g.order_count
          :g.average_numerator_minor!==null||g.average_denominator!==null))return [];
      }else if(Object.keys(g).sort().join()!==(daily?"bucket_since,bucket_until,key,order_count,partial_bucket":"key,order_count"))return [];
    }
    if(v.metric==="status_share"&&total!==v.total_order_count)return [];
    if(dailyShare&&[...days.values()].some(d=>d.statuses.size!==3||d.count!==d.denominator))return [];
    return [{seq:e.seq,value:v}];
  });
}
