export const orderStatuses = ["all", "pending", "completed", "cancelled"] as const;
export type OrderStatus = typeof orderStatuses[number];
export interface OrderCountRequest { since: string; until: string; status: OrderStatus }
export interface OrderCountPlan { request: OrderCountRequest; explanation: string[] }

function timestamp(value: string): number {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,3})?(Z|[+-]\d{2}:\d{2})$/.exec(value);
  if (!m) throw new Error("起止时间必须包含日期、时分秒和时区，例如2026-10-01T00:00:00+08:00。");
  const [year, month, day, hour, minute, second] = m.slice(1, 7).map(Number) as [number, number, number, number, number, number];
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  const zone = m[7]!;
  if (year < 1 || month < 1 || month > 12 || day < 1 || day > days[month - 1]!
    || hour > 23 || minute > 59 || second > 59
    || (zone !== "Z" && (Number(zone.slice(1, 3)) > 14 || Number(zone.slice(4)) > 59
      || (Number(zone.slice(1, 3)) === 14 && Number(zone.slice(4)) !== 0)))) throw new Error("日期、时间或时区无效。");
  const result = Date.parse(value);
  if (!Number.isFinite(result)) throw new Error("日期、时间或时区无效。");
  return result;
}
export function previewOrderCount(input: {since: string; until: string; status: string}): OrderCountPlan {
  const start = timestamp(input.since), end = timestamp(input.until);
  if (end <= start || end - start > 31 * 86400000) throw new Error("创建时间窗口必须为正且不超过31天（UTC时长）。");
  if (!orderStatuses.includes(input.status as OrderStatus)) throw new Error("仅支持全部、待完成、已完成和已取消状态。");
  return {
    request: {since: new Date(start).toISOString(), until: new Date(end).toISOString(), status: input.status as OrderStatus},
    explanation: [
      "按订单创建时间选择左闭右开窗口；起点计入、终点排除，统一转换为UTC。",
      "状态是读取时当前状态，不是历史状态快照；迟到数据和状态变化可能改变重查结果。",
      "每个租户内相同订单ID的完全重复记录去重，ID冲突、无效创建时间/状态或来源截断时不提供数量。",
      "只输出所选状态的订单数量和状态分布；不返回订单明细、金额、收入、排序、同比环比或跨租户合计。",
      "仅独立合成经营profile；执行时校验本人、委托链、精确清单和read_data，读取前后复核，最多100条来源记录，未完整扫描不补零。",
      "预览不执行；请核对后显式登记、执行一次、只读恢复与本人ACK。",
    ],
  };
}
export function proposeOrderCount(text: string): OrderCountPlan {
  if (text.length > 512) throw new Error("统计描述最多512字符。");
  const m = /^统计创建时间从“([^“”]+)”到“([^“”]+)”的(全部|待完成|已完成|已取消)订单数量[。?？]?$/.exec(text.trim());
  if (!m) throw new Error("请使用明确的起止时间与状态：统计创建时间从“带时区起点”到“带时区终点”的已完成订单数量。不支持今天、最近、销售额或跨租户统计。");
  const statuses: Record<string, OrderStatus> = {全部:"all", 待完成:"pending", 已完成:"completed", 已取消:"cancelled"};
  return previewOrderCount({since: m[1]!, until: m[2]!, status: statuses[m[3]!]!});
}
