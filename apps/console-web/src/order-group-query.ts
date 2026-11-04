import {previewOrderCount, type OrderStatus} from "./order-count-query";
export type GroupMetric = "order_count" | "completed_amount" | "completed_average" | "status_share";
export type GroupDimension = "status" | "currency" | "status_currency";
export interface GroupRequest { since:string; until:string; status:OrderStatus; metric:GroupMetric; group_by:GroupDimension }
export function previewOrderGroups(input:{since:string;until:string;status:string;metric:string;group_by:string}):{request:GroupRequest;explanation:string[]} {
  const base = previewOrderCount(input).request;
  if (input.metric === "status_share" && (input.status !== "all" || input.group_by !== "status")) throw new Error("状态占比必须使用全部状态并按状态分组，分母为同窗口全部去重订单数。");
  if (input.metric !== "order_count" && input.metric !== "completed_amount" && input.metric !== "completed_average" && input.metric !== "status_share") throw new Error("仅支持订单数量、金额、平均金额或状态占比。");
  if (!["status","currency","status_currency"].includes(input.group_by)) throw new Error("仅支持状态、币种或状态＋币种分组。");
  if ((input.metric === "completed_amount" || input.metric === "completed_average") && (input.status !== "completed" || input.group_by !== "currency")) throw new Error("金额仅统计已完成订单，必须按币种分别列示，不跨币种合计。");
  return {
    request:{...base,metric:input.metric,group_by:input.group_by as GroupDimension},
    explanation:[
      ...(input.metric === "status_share" ? ["状态占比分母为同一创建窗口全部有效去重订单总数；分子为各当前状态数量，不接受先筛选单一状态。空窗口占比未知，不补0%。"] : []),
      "按创建时间[起点,终点)选择cohort，显式转UTC，最多31天；状态是读取时当前状态，不是历史快照。",
      input.metric === "status_share" ? "固定列出三种当前状态；非空完整窗口中缺失状态为0/全部订单数，空窗口三种占比均未知。来源不完整不提供任何占比。" : "每个租户按订单ID去重；冲突、无效创建时间/状态、来源截断或超过100行时统计未知。仅返回实际存在的分组，空范围为无分组，不捏造0金额。",
      (input.metric === "completed_amount" || input.metric === "completed_average")
        ? "金额为已完成订单的amount_minor，CNY/USD均100最小单位=1主单位；不是实收、收入、利润或GMV。不换汇、不跨币种相加。"
        : "数量按指定状态和分组计算，不把分组数量当作销量或付款笔数。",
      "依赖币种的分组仅支持CNY/USD，所选范围出现无效币种则整组结果未知；按状态计数不依赖金额或币种。",
      ...(["completed_amount","completed_average"].includes(input.metric) ? [
        "平均金额仅在金额全有效时提供，以完整总额最小单位/完成订单数显示精确分数，不使用已知子样本平均，不作舍入。",
        "金额须为非负整数，缺失、布尔、负数、浮点或字符串均不算有效金额；缺失时正式总额未知，保留明确标注的已知小计与有效金额条数/完成订单数。合法零金额保留0。",
      ] : []),
      "独立合成经营profile，仅精确授权用户；不扩旧F4或数量工具权限，不返回原始明细。确认后仍需登记、一次执行、只读恢复和本人ACK。",
    ],
  };
}
export function proposeOrderGroups(text:string) {
  if (text.length > 512) throw new Error("统计描述最多512字符。");
  const share=/^统计创建时间从“([^“”]+)”到“([^“”]+)”的订单状态占比[。?？]?$/.exec(text.trim());
  if (share) return previewOrderGroups({since:share[1]!,until:share[2]!,status:"all",metric:"status_share",group_by:"status"});
  const m=/^统计创建时间从“([^“”]+)”到“([^“”]+)”的(全部|待完成|已完成|已取消)订单(数量|金额|平均金额)，按(状态|币种|状态和币种)分组[。?？]?$/.exec(text.trim());
  if (!m) throw new Error("请使用明确窗口、状态、指标和分组句式；不支持销售额、退款、利润、客户、排行或任意SQL。");
  const statuses:Record<string,OrderStatus>={全部:"all",待完成:"pending",已完成:"completed",已取消:"cancelled"};
  const dimensions:Record<string,GroupDimension>={状态:"status",币种:"currency",状态和币种:"status_currency"};
  return previewOrderGroups({since:m[1]!,until:m[2]!,status:statuses[m[3]!]!,metric:m[4]==="数量"?"order_count":m[4]==="平均金额"?"completed_average":"completed_amount",group_by:dimensions[m[5]!]!});
}
