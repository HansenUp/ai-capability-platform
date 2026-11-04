/** Local synthetic preview only. It never authorizes or executes a query. */
export interface RestrictedQueryDraft {
  view_id: "orders";
  version: string;
  columns: ("item" | "secret")[];
  filters: { column: "item"; value: string }[];
  limit: number;
}

export interface QueryPreview {
  request: RestrictedQueryDraft;
  template: string;
  bindings: { name: string; source: string; value?: string | number }[];
  explanation: string[];
}

const denied = () => new Error("请选择有效合成视图版本、至少一列和1–100整数限行；等值条件最多128字符。");

export function previewRestrictedQuery(input: {
  version: string; columns: string[]; filter: string; limit: number;
}): QueryPreview {
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(input.version)
    || !input.columns.length || new Set(input.columns).size !== input.columns.length
    || input.columns.some(column => column !== "item" && column !== "secret")
    || !Number.isInteger(input.limit) || input.limit < 1 || input.limit > 100
    || input.filter.length > 128) throw denied();
  const columns = input.columns.map(column => column as "item" | "secret");
  const filters: RestrictedQueryDraft["filters"] = input.filter === "" ? [] : [{column: "item", value: input.filter}];
  const request: RestrictedQueryDraft = {view_id: "orders", version: input.version, columns, filters, limit: input.limit};
  const bindings: QueryPreview["bindings"] = [];
  const selections = columns.map(column => {
    if (column === "secret") {
      bindings.push({name: ":mask", source: "固定遮盖值", value: "[REDACTED]"});
      return ':mask AS "secret"';
    }
    return '"item"';
  });
  bindings.push({name: ":tenant", source: "执行时从已验证身份取得，不接受浏览器输入"});
  const predicates = ['typeof("tenant") = \'text\'', 'CAST("tenant" AS TEXT) COLLATE BINARY = :tenant'];
  if (filters.length) {
    predicates.push('"item" = :item');
    bindings.push({name: ":item", source: "item 等值条件，作为参数绑定", value: input.filter});
  }
  bindings.push({name: ":fetch_limit", source: "多取一行以判断截断", value: input.limit + 1});
  return {
    request,
    // Logical names only: the current registered physical view/tenant column
    // and authorization are resolved independently by the execution service.
    template: `SELECT ${selections.join(", ")}\nFROM <已登记的 orders 视图>\nWHERE ${predicates.join("\n  AND ")}\nLIMIT :fetch_limit`,
    bindings,
    explanation: [
      `请求清单版本 ${input.version}；执行时仍需核对当前清单和精确 read_data 策略。`,
      `投影 ${columns.join("、")}。${columns.includes("secret") ? "secret 只返回固定 [REDACTED]，不读取其原值。" : "不请求 secret。"}`,
      filters.length ? "仅按 item 等值匹配；引号、SQL 关键字均属于条件值，不拼接为语句。" : "未设置业务过滤，仍保留服务端租户约束。",
      `最多展示 ${input.limit} 行；内核最多取 ${input.limit + 1} 行判断截断，不保证排序或完整结果。`,
      "这是本机合成逻辑模板，不是数据库执行计划；不读取数据、不调用模型、不授予权限，也不创建 Run。",
    ],
  };
}
