import {previewOrderCount, type OrderStatus} from "./order-count-query";
type Event = {seq:number; type:string; payload:Record<string, unknown>};
export interface CountResult {
  since:string; until:string; status_filter:OrderStatus; view_version:string; policy_version:string;
  read_started:string; read_finished:string; complete:boolean; reason:string|null;
  order_count:number|null; statuses:Record<"pending"|"completed"|"cancelled",number>|null;
}
export function orderCountResults(events: Event[]): {seq:number; value:CountResult}[] {
  const calls = new Map(events.filter(e => e.type === "tool.requested" && e.payload.tool === "analytics.order_count")
    .map(e => [e.payload.call_id, e]));
  return events.flatMap(e => {
    const call = calls.get(e.payload.call_id);
    const v = e.payload.output as CountResult & {profile?:string; snapshot_guaranteed?:boolean} | undefined;
    if (!call || call.seq >= e.seq || e.type !== "tool.result" || e.payload.status !== "ok" || !v
      || v.profile !== "synthetic-order-count-v1" || v.snapshot_guaranteed !== false
      || typeof v.complete !== "boolean" || ![v.view_version,v.policy_version,v.read_started,v.read_finished].every(x => typeof x === "string" && !!x)) return [];
    try {
      const expected = previewOrderCount((call.payload.effective_input ?? call.payload.input) as {since:string;until:string;status:string}).request;
      const received = previewOrderCount({since:v.since,until:v.until,status:v.status_filter}).request;
      if (JSON.stringify(expected) !== JSON.stringify(received)) return [];
    } catch { return []; }
    if (v.complete) {
      if (v.reason !== null || !Number.isInteger(v.order_count) || v.order_count! < 0 || v.order_count! > 100 || !v.statuses
        || Object.keys(v.statuses).sort().join() !== "cancelled,completed,pending"
        || !Object.values(v.statuses).every(n => Number.isInteger(n) && n >= 0 && n <= 100)
        || Object.values(v.statuses).reduce((a,b) => a+b,0) !== v.order_count
        || (v.status_filter !== "all" && Object.entries(v.statuses).some(([s,n]) => s !== v.status_filter && n !== 0))) return [];
    } else if (v.order_count !== null || v.statuses !== null || typeof v.reason !== "string" || !v.reason) return [];
    return [{seq:e.seq,value:v}];
  });
}
