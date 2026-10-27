export function configObject(text: string): Record<string, unknown> {
  const value: unknown = JSON.parse(text);
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("config 必须是 JSON 对象");
  return value as Record<string, unknown>;
}

export function stringList(text: string): string[] {
  return [...new Set(text.split(/[\n,]/).map(value => value.trim()).filter(Boolean))];
}

export function updateConfig(text: string, key: "tools" | "skills" | "redlines", value: unknown): string {
  const config = configObject(text);
  if (key === "redlines") {
    const previous = config.redlines;
    if (previous !== undefined && (!previous || typeof previous !== "object" || Array.isArray(previous)))
      throw new Error("已有 redlines 不是对象，请先在 JSON 中修正");
    config.redlines = { ...(previous as Record<string, unknown> | undefined), deny_tools: value };
  } else config[key] = value;
  return JSON.stringify(config, null, 2);
}

function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical((value as Record<string, unknown>)[key])}`).join(",")}}`;
  return JSON.stringify(value) ?? "undefined";
}

export interface Difference { path: string; before: string; after: string }
export function differences(before: unknown, after: unknown, path = ""): Difference[] {
  if (canonical(before) === canonical(after)) return [];
  const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
  if (object(before) && object(after)) {
    return [...new Set([...Object.keys(before), ...Object.keys(after)])].sort().flatMap(key =>
      differences(before[key], after[key], `${path}/${key.replace(/~/g, "~0").replace(/\//g, "~1")}`));
  }
  return [{ path: path || "/", before: before === undefined ? "（缺失）" : JSON.stringify(before, null, 2), after: after === undefined ? "（缺失）" : JSON.stringify(after, null, 2) }];
}
