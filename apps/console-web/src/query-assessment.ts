import {proposeRestrictedQuery, QueryLanguageError} from "./restricted-query-language";
import {proposeOrderCount} from "./order-count-query";
import {proposeOrderGroups} from "./order-group-query";

export const queryCapabilities = [
  {id:"detail",name:"商品字段明细",route:"/manage/data-views",scope:"商品精确匹配、固定遮盖、1–100行",example:"查一下商品a1的订单，最多10条"},
  {id:"count",name:"日期、状态与数量",route:"/manage/order-counts",scope:"显式带时区创建窗口、当前状态、去重数量",example:"统计创建时间从“2026-10-01T00:00:00Z”到“2026-10-02T00:00:00Z”的全部订单数量"},
  {id:"groups",name:"金额与受限分组",route:"/manage/order-groups",scope:"数量按状态/币种；已完成金额及均值分币种",example:"统计创建时间从“2026-10-01T00:00:00Z”到“2026-10-02T00:00:00Z”的已完成订单金额，按币种分组"},
] as const;
export type AssessmentStatus="supported"|"needs_clarification"|"not_supported"|"scope_rejected";
export interface QueryAssessment {status:AssessmentStatus;reason:string;missing:string[];target?:string;route?:string;request?:unknown}
export function assessQuery(text:string,version="v1"):QueryAssessment {
  if(text.length>512)return {status:"not_supported",reason:"描述超过512字符，请缩短后重新评估。",missing:[]};
  if(!text.trim())return {status:"needs_clarification",reason:"请输入希望查询的内容。",missing:["查询内容"]};
  const parsers=[()=>proposeRestrictedQuery(text,version).request,()=>proposeOrderCount(text).request,()=>proposeOrderGroups(text).request];
  let detailFailure:unknown;
  for(let i=0;i<parsers.length;i++){
    try{const request=parsers[i]!();const c=queryCapabilities[i]!;return {status:"supported",reason:"当前合成契约能够生成候选；尚未授权或执行。",missing:[],target:c.name,route:c.route,request};}
    catch(e){if(i===0)detailFailure=e;}
  }
  // Diagnose only after full parsers rejected: quoted item literals are data.
  const semantic=text.replace(/“[^“”]*”|"[^"]*"/g,"<值>");
  if(/跨租户|所有租户|其他租户|绕过权限|忽略权限|全公司/.test(semantic))return {status:"scope_rejected",reason:"该描述要求越过当前租户或权限边界，不生成候选。实际身份授权仍由执行服务核验。",missing:[]};
  if(/\b(select|insert|update|delete|drop|union|join)\b/i.test(semantic)||/退款|利润|GMV|客户|员工|排行|同比|环比|日期分桶/.test(semantic))return {status:"not_supported",reason:"当前受限工具尚未支持所述指标、维度或SQL操作，不能忽略这些条件执行。",missing:[]};
  if(/销售额|收入|实收|总额/.test(semantic))return {status:"needs_clarification",reason:"请明确业务指标。当前订单金额不代表实收、收入或利润。",missing:["指标定义（例如已完成订单金额）","币种分别列示","明确创建时间窗口与时区"]};
  if(/今天|昨天|本月|上月|最近|本周|我的/.test(semantic))return {status:"needs_clarification",reason:"请明确绝对日期、时区或主体范围；不会自动推断。",missing:["带时区创建起止时间","状态或主体范围"]};
  if(/统计/.test(semantic)&&/订单/.test(semantic)){
    const times=text.match(/\d{4}-\d{2}-\d{2}T/g)??[];
    if(times.length<2)return {status:"needs_clarification",reason:"请补充明确的统计范围，使用对应入口填写参数。",missing:["带时区创建起止时间","当前状态","指标与分组方式"]};
    return {status:"needs_clarification",reason:"日期、状态、指标组合或表达未通过校验；请在统计页核对参数，不生成近似请求。",missing:["有效日期（最长31天）","受支持的状态、指标与分组组合"]};
  }
  if(/订单|商品|查询|查看|查一下/.test(semantic)&&detailFailure instanceof QueryLanguageError&&["LIMIT_REQUIRED","LIMIT_RANGE"].includes(detailFailure.code))return {status:"needs_clarification",reason:detailFailure.message,missing:["最多返回1–100行及明确明细句式"]};
  return {status:"not_supported",reason:"当前句式或条件尚未支持，请参考能力表；未生成或执行查询。",missing:[]};
}
