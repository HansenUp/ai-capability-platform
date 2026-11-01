/** Local preview only: this document is not a saved dataset or evaluation evidence. */
export interface SyntheticCatalog { scenario_id: string; cases: { id: string }[] }
export const catalogRevision = "synthetic-catalog-1";
export function previewSelection(text: string, catalogs: readonly SyntheticCatalog[]) {
  if (new TextEncoder().encode(text).length > 16384) throw new Error("清单超过16KiB");
  // JSON.parse silently overwrites duplicate keys. Reject them before interpreting input.
  const keys = new Set<string>();
  for (const match of text.matchAll(/"(?:[^"\\]|\\.)*"\s*:/g)) {
    const key: string = JSON.parse(match[0].replace(/\s*:$/, ""));
    if (keys.has(key)) throw new Error("清单含重复字段");
    keys.add(key);
  }
  const value: unknown = JSON.parse(text);
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("清单必须为JSON对象");
  const row = value as Record<string, unknown>;
  const expected = ["schema_version", "scenario_id", "catalog_revision", "case_ids"];
  if (Object.keys(row).length !== expected.length || expected.some(key => !Object.prototype.hasOwnProperty.call(row, key))) throw new Error("清单字段不匹配");
  if (row.schema_version !== 1 || row.catalog_revision !== catalogRevision) throw new Error("清单或目录版本不匹配");
  const catalog = catalogs.find(item => item.scenario_id === row.scenario_id);
  if (!catalog) throw new Error("未知合成场景");
  const ids = row.case_ids;
  if (!Array.isArray(ids) || !ids.length || ids.some(id => typeof id !== "string" || !catalog.cases.some(item => item.id === id))) throw new Error("请选择有效的固定合成用例");
  if (new Set(ids).size !== ids.length) throw new Error("用例不能重复");
  return { scenario_id: catalog.scenario_id, catalog_revision: catalogRevision, case_ids: ids as string[] };
}
