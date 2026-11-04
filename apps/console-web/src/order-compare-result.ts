import {previewOrderCompare,type OrderCompareRequest} from "./order-compare-query";
type Event={seq:number;type:string;payload:Record<string,unknown>};
export interface OrderCompareResult extends Omit<OrderCompareRequest,"status"> {profile:string;status_filter:string;view_version:string;policy_version:string;read_started:string;read_finished:string;snapshot_guaranteed:false;complete:boolean;reason:string|null;baseline_count:number|null;current_count:number|null;delta_count:number|null;relative_numerator:number|null;relative_denominator:number|null;data_class?:string}
const count=(v:unknown):v is number=>typeof v==="number"&&Number.isInteger(v)&&v>=0&&v<=100;
const metricFields=["baseline_count","current_count","delta_count","relative_numerator","relative_denominator"] as const;
const allowed=new Set(["profile","status_filter","view_version","policy_version","read_started","read_finished","snapshot_guaranteed","complete","reason","baseline_since","baseline_until","current_since","current_until","data_class",...metricFields]);
export function orderCompareResults(events:Event[]):{seq:number;value:OrderCompareResult}[]{
  const calls=new Map(events.filter(e=>e.type==="tool.requested"&&e.payload.tool==="analytics.order_compare").map(e=>[e.payload.call_id,e]));
  return events.flatMap(e=>{
    const call=calls.get(e.payload.call_id),v=e.payload.output as OrderCompareResult|undefined;
    if(!call||call.seq>=e.seq||e.type!=="tool.result"||e.payload.status!=="ok"||!v||v.profile!=="synthetic-order-compare-v1"||v.snapshot_guaranteed!==false||typeof v.complete!=="boolean"||Object.keys(v).some(k=>!allowed.has(k))
      ||![v.view_version,v.policy_version,v.read_started,v.read_finished].every(s=>typeof s==="string"&&!!s))return [];
    try{
      const expected=previewOrderCompare((call.payload.effective_input??call.payload.input) as OrderCompareRequest).request;
      const received=previewOrderCompare({baseline_since:v.baseline_since,baseline_until:v.baseline_until,current_since:v.current_since,current_until:v.current_until,status:v.status_filter}).request;
      if(JSON.stringify(expected)!==JSON.stringify(received))return [];
    }catch{return [];}
    if(!v.complete)return typeof v.reason==="string"&&!!v.reason&&metricFields.every(k=>v[k]===null)?[{seq:e.seq,value:v}]:[];
    if(v.reason!==null||!count(v.baseline_count)||!count(v.current_count)||v.baseline_count+v.current_count>100||v.delta_count!==v.current_count-v.baseline_count)return [];
    if(v.baseline_count===0?(v.relative_numerator!==null||v.relative_denominator!==null):(v.relative_numerator!==v.delta_count||v.relative_denominator!==v.baseline_count))return [];
    return [{seq:e.seq,value:v}];
  });
}
