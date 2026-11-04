import { previewRestrictedQuery, type QueryPreview } from "./restricted-query-preview";

/** Deliberately bounded local language adapter; never executes or infers permissions. */
export function proposeRestrictedQuery(text: string, version: string): QueryPreview {
  if (text.length > 512) throw new Error("查询描述最多512字符。");
  // Consume the entire utterance. Values are quoted literals, never instructions.
  const match = /^查询订单(?:，商品等于“([^“”\r\n]{1,128})”)?，显示商品(和遮盖信息)?，最多([1-9][0-9]{0,2})行[。]?$/.exec(text.trim());
  if (!match) throw new Error("暂不支持此表达。请使用示例句式；仅支持商品等值、商品/遮盖信息投影和1–100行，不支持统计、排序、跨租户或SQL。");
  return previewRestrictedQuery({
    version, columns: match[2] ? ["item", "secret"] : ["item"],
    filter: match[1] ?? "", limit: Number(match[3]),
  });
}
