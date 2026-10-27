import { profile } from "./support.mjs";
export const version = { agent_id: "synthetic-agent", version: "1", status: "draft", system_prompt: "synthetic", model_alias: "mock",
  harness: "mock", config: { tools: ["echo"], extension: { retained: true } }, eval_run_ref: null, canary: { percent: 100, allowlist: [] } };
export const flag = { mode: "flag", min_items: 30, min_pass_rate: 0.8, datasets: [] };
export const node = { node_id: "synthetic-node", topology: "test", labels: ["general"], version: "test", last_seen: "2026-10-21T00:00:00Z",
  load: 0, qps: 0, healthy: true, ready: true };
export const groups = [{ name: "general", description: "default", created_at: "2026-10-21T00:00:00Z", nodes: [node.node_id] },
  { name: "empty", description: "synthetic", created_at: "2026-10-21T00:00:00Z", nodes: [] },
  { name: "occupied", description: "synthetic member", created_at: "2026-10-21T00:00:00Z", nodes: [node.node_id] }];
export const routing = { groups: { general: [node.node_id], empty: [] }, bindings: {}, default_group: "general" };
export function receipt(status = "pending") {
  return { id: "synthetic-command", node_id: node.node_id, tenant_id: "default", type: "reload_config", status,
    created_at: "2026-10-21T00:00:00Z", acked_at: status === "pending" ? null : "2026-10-21T00:01:00Z" };
}
export async function mount(engine, view, action) {
  const context = await engine.newContext();
  await context.addInitScript(() => localStorage.setItem("console.operator", JSON.stringify({ user: "ops", role: "PLATFORM_ADMIN", tenant: "default" })));
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  const calls = [];
  let handler = async () => false;
  await page.route("**/console/**", async route => {
    const request = route.request();
    const record = { path: new URL(request.url()).pathname.replace(/^\/console/, ""), url: new URL(request.url()),
      method: request.method(), body: request.postDataJSON(), headers: request.headers() };
    calls.push(record);
    if (await handler(route, record)) return;
    if (record.method === "GET") {
      const defaults = { "/agents": [version], "/nodes": [node], "/node-groups/details": groups, "/routing": routing,
        "/agents/synthetic-agent/eval-gate": flag };
      if (Object.hasOwn(defaults, record.path)) return route.fulfill({ json: defaults[record.path] });
    }
    return route.fulfill({ status: 404, json: { detail: "Unconfigured isolated component request" } });
  });
  try {
    await page.goto(`${profile.url}/tests/browser/management.html?view=${view}`);
    if (view === "agents") await page.getByRole("row").filter({ hasText: version.agent_id }).waitFor();
    else await page.getByRole("status").filter({ hasText: "已刷新" }).waitFor();
    await action({ page, calls, handle: value => { handler = value; }, writes: () => calls.filter(row => row.method !== "GET") });
  } finally { await context.close(); }
}
export async function release(page) {
  await page.getByRole("row").filter({ hasText: version.agent_id }).getByRole("button", { name: "评测与发布", exact: true }).click();
  await page.getByText(/闸门 .* · 最少/).waitFor();
}
export async function previewCommand(page) {
  await page.getByRole("combobox", { name: /^目标节点/ }).selectOption(node.node_id);
  await page.getByLabel("轮询秒数", { exact: true }).fill("7");
  await page.getByRole("button", { name: "校验并预览指令", exact: true }).click();
  await page.getByRole("group", { name: "确认下发节点指令", exact: true }).waitFor();
}
