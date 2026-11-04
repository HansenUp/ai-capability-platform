import { previewRestrictedQuery, type QueryPreview } from "./restricted-query-preview";

export type QueryLanguageCode = "EMPTY" | "TOO_LONG" | "LIMIT_REQUIRED" | "LIMIT_RANGE"
  | "UNSUPPORTED_SCOPE" | "UNSUPPORTED_AGGREGATION" | "UNSUPPORTED_ORDER"
  | "UNSUPPORTED_FIELD" | "UNSUPPORTED_SQL" | "UNSUPPORTED_EXPRESSION";

export class QueryLanguageError extends Error {
  constructor(public readonly code: QueryLanguageCode, message: string, public readonly suggestion: string) {
    super(message);
    this.name = "QueryLanguageError";
  }
}

const example = "查一下商品a1的订单，最多10条";
function reject(code: QueryLanguageCode, message: string, suggestion = `请改写为：${example}。`): never {
  throw new QueryLanguageError(code, message, suggestion);
}

/** Bounded local language adapter. Full matches only; never executes or infers permissions. */
export function proposeRestrictedQuery(text: string, version: string): QueryPreview {
  if (text.length > 512) reject("TOO_LONG", "查询描述最多512字符。");
  const input = text.trim();
  if (!input) reject("EMPTY", "请输入要查询的订单明细。");
  // Quoted values stay byte-for-byte intact, including punctuation and SQL-like text.
  const literal = '(?:“([^“”\\r\\n]{1,128})”|"([^"\\r\\n]{1,128})"|([A-Za-z0-9][A-Za-z0-9_.-]{0,127}))';
  const bounded = (filter: string, masked: boolean, limit: string) => {
    const count = Number(limit);
    if (!Number.isInteger(count) || count < 1 || count > 100) reject("LIMIT_RANGE", "最多返回行数须为1–100的整数。", "请明确填写最多1至100行；不支持全量下载。");
    return previewRestrictedQuery({version, columns: masked ? ["item", "secret"] : ["item"], filter, limit: count});
  };
  const canonical = /^查询订单(?:[，,]\s*商品等于“([^“”\r\n]{1,128})”)?[，,]\s*显示商品(和遮盖信息)?[，,]\s*最多([0-9]+)(?:行|条)[。?？]?$/u.exec(input);
  if (canonical) return bounded(canonical[1] ?? "", !!canonical[2], canonical[3]!);
  const detail = new RegExp(`^(?:查询|查看|查一下)(?:商品${literal}的)?订单(?:[，,]\\s*(显示商品|包含遮盖信息))?[，,]\\s*最多([0-9]+)(?:行|条)[。?？]?$`, "u").exec(input);
  if (detail) return bounded(detail[1] ?? detail[2] ?? detail[3] ?? "", detail[4] === "包含遮盖信息", detail[5]!);

  // Diagnose only rejected utterances; never reinterpret unsupported clauses or
  // scan a successfully parsed literal as an instruction.
  const semantics = input.replace(/“[^“”]*”|"[^"]*"/gu, "<值>");
  if (/跨租户|所有租户|其他租户|全公司|我的|本人|忽略权限|绕过权限/u.test(semantics)) reject("UNSUPPORTED_SCOPE", "不支持自行指定租户、所有者或绕过权限。", "当前仅按服务端已验证身份和精确read_data策略限制范围；不能把“我的”推断为商品或租户条件。");
  if (/\b(?:select|insert|update|delete|drop|union|join)\b/iu.test(semantics)) reject("UNSUPPORTED_SQL", "不接受SQL或数据修改指令。", "请描述只读订单明细；查询候选仅生成受限结构化请求。");
  if (/统计|总额|销售额|平均|合计|多少|数量|分组|占比/u.test(semantics)) reject("UNSUPPORTED_AGGREGATION", "当前视图不支持统计或金额计算。", "本入口只能查询商品明细，不能把截断结果当作总量；经营分析需使用已批准的独立场景与指标口径。");
  if (/排序|倒序|升序|降序|最新|最早|前[0-9]+/u.test(semantics)) reject("UNSUPPORTED_ORDER", "当前查询不保证排序，无法回答最新或前N条。", "可改用“最多N条”明确结果上限；结果不代表排序或完整数据。");
  if (/金额|日期|今天|昨天|最近|状态|客户|时间|原始|未遮盖|secret|tenant/u.test(semantics)) reject("UNSUPPORTED_FIELD", "当前视图仅支持商品等值和固定遮盖信息。", "请移除当前视图不支持的字段、时间或原值要求；不能忽略这些条件继续查询。");
  if (!/最多/u.test(semantics)) reject("LIMIT_REQUIRED", "请明确最多返回多少行。", "请在描述末尾补充“，最多10条”（1–100），不会自动查询全部数据。");
  reject("UNSUPPORTED_EXPRESSION", "暂不支持此表达，未生成查询候选。", `示例：${example}；多条件、模糊匹配、否定和未识别语义需要重新描述，不能自动忽略。`);
}
