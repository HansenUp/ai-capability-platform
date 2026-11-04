import {previewOrderCount,type OrderStatus} from "./order-count-query";
export interface OrderCompareRequest {baseline_since:string;baseline_until:string;current_since:string;current_until:string;status:OrderStatus}
const fields=["baseline_since","baseline_until","current_since","current_until","status"];
export const compareCapability={id:"compare",name:"双窗口订单数量对比",route:"/manage/order-compare",scope:"等长不重叠创建窗口、同一当前状态、精确差值/基准比例",example:'对比基准创建时间从“2026-10-01T00:00:00Z”到“2026-10-02T00:00:00Z”与当前创建时间从“2026-10-02T00:00:00Z”到“2026-10-03T00:00:00Z”的全部订单数量'} as const;
export function previewOrderCompare(input:{baseline_since:string;baseline_until:string;current_since:string;current_until:string;status:string}) {
  if(!input||Object.keys(input).sort().join()!==[...fields].sort().join())throw new Error("只接受基准/当前两组明确时间和一个共同状态。");
  const baseline=previewOrderCount({since:input.baseline_since,until:input.baseline_until,status:input.status}).request;
  const current=previewOrderCount({since:input.current_since,until:input.current_until,status:input.status}).request;
  if(Date.parse(baseline.until)-Date.parse(baseline.since)!==Date.parse(current.until)-Date.parse(current.since))throw new Error("两窗必须有相同的经过时长，各最长31天。");
  if(Date.parse(baseline.until)>Date.parse(current.since))throw new Error("基准终点必须早于或等于当前起点；不允许重叠或颠倒。");
  return {request:{baseline_since:baseline.since,baseline_until:baseline.until,current_since:current.since,current_until:current.until,status:baseline.status},explanation:[
    "基准窗口在前、当前窗口在后；显式时区转UTC，两个窗口等长、互不重叠，各最长31经过天；可有间隔，不推断今天/同比/环比。",
    "两个创建窗口均为[起点,终点)，相接边界只属于后一窗口；按订单ID去重、同一当前状态筛选，不表示历史状态快照。",
    "两窗计数来自同一完整授权来源，返回前再次核验权限和来源；不是两个独立工具结果拼接，也不承诺数据库事务快照。",
    "当前数量－基准数量为带符号差值；相对变化以差值/基准数量的精确分数显示，不自行舍入百分比。基准为0时比例未知，保留真实计数和差值。",
    "只支持数量，不解释成销量或付款笔数，不涉及金额/换汇；来源超过100行、截断、冲突或无效关键事实时两窗均未知，不补0。",
    "本人精确授权仍由服务端校验，不扩大旧工具权限、不返回明细；显式登记执行一次、同Run匹配工具结果、只读恢复和本人ACK。",
  ]};
}
export function proposeOrderCompare(text:string){
  if(text.length>512)throw new Error("对比描述最多512字符。");
  const m=/^对比基准创建时间从“([^“”]+)”到“([^“”]+)”与当前创建时间从“([^“”]+)”到“([^“”]+)”的(全部|待完成|已完成|已取消)订单数量[。?？]?$/.exec(text.trim());
  if(!m)throw new Error("请使用明确基准/当前创建窗口和共同状态的数量对比句式；不忽略额外条件。");
  const statuses:Record<string,OrderStatus>={全部:"all",待完成:"pending",已完成:"completed",已取消:"cancelled"};
  return previewOrderCompare({baseline_since:m[1]!,baseline_until:m[2]!,current_since:m[3]!,current_until:m[4]!,status:statuses[m[5]!]!});
}
