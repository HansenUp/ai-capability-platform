import {previewOrderCount} from "./order-count-query";
export type BucketTimezone="UTC"|"UTC+08:00";
export function dailyBounds(day:string,since:string,until:string,zone:string) {
  if(zone!=="UTC"&&zone!=="UTC+08:00")throw new Error("请选择UTC或固定UTC+08:00，不推断浏览器时区或夏令时。");
  if(!/^\d{4}-\d{2}-\d{2}$/.test(day))throw new Error("日桶日期无效。");
  const date=Date.parse(day+"T00:00:00Z");
  if(!Number.isFinite(date)||new Date(date).toISOString().slice(0,10)!==day)throw new Error("日桶日期无效。");
  const start=date-(zone==="UTC+08:00"?8*3600000:0),end=start+86400000;
  const a=Date.parse(since),b=Date.parse(until),left=Math.max(start,a),right=Math.min(end,b);
  if(!Number.isFinite(left)||!Number.isFinite(right)||left>=right)throw new Error("日桶在请求窗口之外。");
  return {bucket_since:new Date(left).toISOString(),bucket_until:new Date(right).toISOString(),partial_bucket:a>start||b<end};
}
export function previewDailyGroups(input:{since:string;until:string;status:string;metric:string;group_by:string;bucket_timezone?:string}) {
  const base=previewOrderCount(input).request;
  if(input.bucket_timezone!=="UTC"&&input.bucket_timezone!=="UTC+08:00")throw new Error("按日分组必须显式选择UTC或固定UTC+08:00。");
  if(input.metric==="order_count"){
    if(input.group_by!=="day"&&input.group_by!=="day_status")throw new Error("按日数量仅支持日或日＋状态维度。");
  }else if(input.metric==="completed_amount"||input.metric==="completed_average"){
    if(input.status!=="completed"||input.group_by!=="day_currency")throw new Error("按日金额/均值仅支持已完成订单，按日和币种分别列示。");
  }else throw new Error("当前按日分组不支持状态占比或其他指标。");
  return {request:{...base,metric:input.metric as "order_count"|"completed_amount"|"completed_average",group_by:input.group_by as "day"|"day_currency"|"day_status",bucket_timezone:input.bucket_timezone as BucketTimezone},explanation:[
    `分桶时区明确为${input.bucket_timezone}；固定偏移，不默认浏览器时区，不处理夏令时或组织业务日历。`,
    "按创建时间[起点,终点)选订单，再按所选时区的[当日00:00,次日00:00)分桶；午夜订单计入新一天，终点订单排除。",
    "每个桶只统计其与查询窗口的交集；首尾不足整天会标注实际UTC起止区间和非整日，不把局部窗口当完整一天。",
    "仅返回实际有订单的日桶，不补空日或0金额；最多31天窗口可能相交32个日历日。来源仍须完整且不超过100条。",
    ...(input.group_by==="day_status"?["日＋状态只统计订单数量，状态为读取时当前状态；只列实际存在的日期/状态组合，不补空日期或零行。完整来源下未列出的组合无匹配订单；来源不完整整次未知。"]:[]),
    "按日数量不依赖金额/币种；金额及均值只统计已完成订单，按日＋CNY/USD分开，不跨币种合计或推断收入。",
    "每个日桶和币种独立核验金额完整性：缺失时正式总额/均值未知，保留已知小计与覆盖率；均值为完整总额/订单数的精确分数。",
    "仍需本人授权、显式登记执行、结果核对及ACK；编辑候选不更改已登记请求，重载不自动重发。",
  ]};
}
