import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import { browser } from "./support.mjs";
import { groups, mount, node, previewCommand, receipt } from "./management-fixture.mjs";
let engine;
before(async () => { engine = await browser(); });
after(async () => { await engine?.close(); });
async function changeRole(page, role) {
  const refreshed = page.waitForResponse(response => response.url().endsWith("/console/routing") && response.request().headers()["x-console-role"] === role);
  await page.getByRole("combobox", { name: /^角色/ }).selectOption(role);
  await refreshed;
  await page.getByRole("status").filter({ hasText: "已刷新" }).waitFor();
}

test("节点组件：分组创建与编辑说明路径分离，保存后刷新且名称只读", async () => mount(engine, "nodes", async ({ page, writes, handle }) => {
  handle(async (route, row) => {
    if (row.method !== "GET") { await route.fulfill({ json: {} }); return true; }
    return false;
  });
  await page.getByLabel("分组名称", { exact: true }).fill("synthetic-new");
  await page.getByLabel("说明", { exact: true }).fill("synthetic description");
  await page.getByRole("button", { name: "创建分组", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "已保存" }).waitFor();
  assert.deepEqual([writes()[0].method, writes()[0].path, writes()[0].body], ["POST", "/node-groups", { name: "synthetic-new", description: "synthetic description" }]);
  await page.getByRole("button", { name: "编辑分组 empty", exact: true }).click();
  assert.equal(await page.getByLabel("分组名称", { exact: true }).getAttribute("readonly"), "");
  await page.getByLabel("说明", { exact: true }).fill("updated synthetic");
  const saved = page.waitForResponse(response => response.request().method() === "PUT");
  await page.getByRole("button", { name: "保存说明", exact: true }).click();
  await saved;
  assert.deepEqual([writes()[1].method, writes()[1].path, writes()[1].body], ["PUT", "/node-groups/empty", { description: "updated synthetic" }]);
  assert.equal(writes().length, 2);
}));

test("节点组件：默认组与非空组禁止删除，空组取消无写入、确认后刷新", async () => mount(engine, "nodes", async ({ page, writes, handle }) => {
  assert.equal(await page.getByRole("button", { name: "删除分组 general", exact: true }).isDisabled(), true);
  assert.equal(await page.getByRole("button", { name: "删除分组 occupied", exact: true }).isDisabled(), true);
  await page.getByRole("button", { name: "删除分组 empty", exact: true }).click();
  await page.getByRole("button", { name: "取消删除", exact: true }).click();
  assert.equal(writes().length, 0);
  let deleted = false;
  handle(async (route, row) => {
    if (row.method === "DELETE") { deleted = true; await route.fulfill({ json: {} }); return true; }
    if (deleted && row.path === "/node-groups/details") { await route.fulfill({ json: groups.filter(group => group.name !== "empty") }); return true; }
    return false;
  });
  await page.getByRole("button", { name: "删除分组 empty", exact: true }).click();
  await page.getByRole("button", { name: "确认删除", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "已保存" }).waitFor();
  assert.deepEqual(writes().map(row => [row.method, row.path]), [["DELETE", "/node-groups/empty"]]);
  assert.equal(await page.getByRole("button", { name: "删除分组 empty", exact: true }).count(), 0);
  await changeRole(page, "TENANT_ADMIN");
  assert.equal(await page.getByLabel("分组名称", { exact: true }).count(), 0);
  assert.equal(await page.getByRole("button", { name: `调整节点 ${node.node_id} 分组`, exact: true }).isDisabled(), true);
}));

test("节点组件：节点至少一组、成员与租户路由绑定精确参数及二次确认", async () => mount(engine, "nodes", async ({ page, writes, handle }) => {
  handle(async (route, row) => {
    if (row.method !== "GET") { await route.fulfill({ json: {} }); return true; }
    return false;
  });
  await page.getByRole("button", { name: `调整节点 ${node.node_id} 分组`, exact: true }).click();
  const membership = page.getByRole("group", { name: `调整 ${node.node_id} 的分组（至少一个）`, exact: true });
  await membership.getByLabel("general", { exact: true }).uncheck();
  assert.equal(await page.getByRole("button", { name: "保存节点分组", exact: true }).isDisabled(), true);
  await membership.getByLabel("empty", { exact: true }).check();
  await page.getByRole("button", { name: "保存节点分组", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "已保存" }).waitFor();
  assert.deepEqual(writes()[0].body, { labels: ["empty"] });
  assert.equal(writes()[0].path, `/nodes/${node.node_id}/groups`);
  const binding = page.getByRole("group", { name: "配置本租户绑定", exact: true });
  await binding.getByLabel("Agent ID", { exact: true }).fill("synthetic-agent");
  await binding.getByLabel("general", { exact: true }).check();
  assert.equal(await binding.getByLabel("general", { exact: true }).isChecked(), true);
  assert.deepEqual(await binding.locator('input[type="number"]').evaluateAll(elements => elements.map(element => element.getAttribute("aria-label"))), ["general 权重"]);
  await binding.getByLabel("general", { exact: true }).uncheck();
  assert.equal(await page.getByLabel("general 权重", { exact: true }).count(), 0);
  await binding.getByLabel("general", { exact: true }).check();
  await page.getByLabel("general 权重", { exact: true }).fill("35");
  await page.getByRole("button", { name: "保存路由绑定", exact: true }).click();
  assert.equal(writes().length, 1);
  const sent = page.waitForResponse(response => response.request().method() === "PUT" && response.url().endsWith("/bindings"));
  await page.getByRole("button", { name: "确认保存绑定", exact: true }).click();
  await sent;
  assert.deepEqual(writes()[1].body, [{ node_group: "general", weight: 35 }]);
  assert.equal(writes()[1].headers["x-console-tenant"], "default");
}));

test("节点组件：修改参数撤销指令预览，202 仅入队，手动读取同 ID 回执不重发", async () => mount(engine, "nodes", async ({ page, writes, handle }) => {
  let status = "pending";
  handle(async (route, row) => {
    if (row.method === "POST") { await route.fulfill({ status: 202, json: { id: "synthetic-command" } }); return true; }
    if (row.path.endsWith("/commands/synthetic-command")) { await route.fulfill({ json: receipt(status) }); return true; }
    return false;
  });
  await previewCommand(page);
  assert.equal(writes().length, 0);
  await page.getByLabel("轮询秒数", { exact: true }).fill("8");
  assert.equal(await page.getByRole("button", { name: "确认下发一次", exact: true }).count(), 0);
  await page.getByRole("button", { name: "校验并预览指令", exact: true }).click();
  await page.getByRole("button", { name: "确认下发一次", exact: true }).click();
  await page.getByText("reload_config / pending", { exact: true }).waitFor();
  assert.equal(writes().length, 1);
  assert.deepEqual(writes()[0].body, { type: "reload_config", args: { poll_interval_s: 8 } });
  assert.equal(writes()[0].path, `/nodes/${node.node_id}/commands`);
  assert.match(await page.getByRole("status").filter({ hasText: "已入队" }).innerText(), /synthetic-command/);
  status = "ok";
  await page.getByRole("button", { name: "查询回执", exact: true }).click();
  await page.getByText("reload_config / ok", { exact: true }).waitFor();
  assert.equal(writes().length, 1);
  await changeRole(page, "TENANT_ADMIN");
  const restart = page.getByRole("option", { name: "graceful_restart（平台管理员）", exact: true });
  assert.equal(await restart.evaluate(element => element.disabled), true, await restart.evaluate(element => element.outerHTML));
  assert.equal(await restart.evaluate(element => element.matches(":disabled")), true);
}));

test("节点组件：未知下发经审计候选定位回执，仍锁定不重发且不跨租户对账", async () => mount(engine, "nodes", async ({ page, writes, handle }) => {
  handle(async (route, row) => {
    if (row.method === "POST") { await route.abort("failed"); return true; }
    if (row.path === "/audit/search") {
      await route.fulfill({ json: { order: "newest_first", next_before_id: null, items: [{ id: 1, ts: "2026-10-21T00:00:00Z", tenant: "default",
        actor: "ops", action: "node.command", object: node.node_id, detail: { id: "synthetic-command", type: "reload_config", args: { poll_interval_s: 7 } } }] } }); return true;
    }
    if (row.path.endsWith("/commands/synthetic-command")) { await route.fulfill({ json: receipt("ok") }); return true; }
    return false;
  });
  await previewCommand(page);
  await page.getByRole("button", { name: "确认下发一次", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "本页已锁定再次下发" }).waitFor();
  await page.getByRole("button", { name: "只读定位未知下发审计", exact: true }).click();
  await page.getByText("类型与参数相同（非唯一证明）", { exact: false }).waitFor();
  await page.getByRole("button", { name: "按候选 ID 查询回执", exact: true }).click();
  await page.getByText("reload_config / ok", { exact: true }).waitFor();
  assert.equal(await page.getByRole("button", { name: "校验并预览指令", exact: true }).isDisabled(), true);
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "已刷新" }).waitFor();
  assert.equal(await page.getByRole("button", { name: "校验并预览指令", exact: true }).isDisabled(), true);
  await page.getByLabel("租户", { exact: true }).fill("other");
  await page.getByLabel("租户", { exact: true }).blur();
  assert.equal(await page.getByRole("button", { name: "只读定位未知下发审计", exact: true }).isDisabled(), true);
  assert.equal(await page.getByText("reload_config / ok", { exact: true }).count(), 0);
  assert.equal(writes().length, 1);
}));

test("节点组件：历史分页精确游标、拒绝跨租户页并清空旧结果", async () => mount(engine, "nodes", async ({ page, calls, writes, handle }) => {
  handle(async (route, row) => {
    if (row.path === `/nodes/${node.node_id}/command-history`) {
      const more = row.url.searchParams.has("before_id");
      await route.fulfill({ json: { tenant_id: more ? "foreign" : "default", node_id: node.node_id, order: "newest_first", next_before_id: more ? null : 10,
        items: [{ ...receipt(), record_id: 11, created_by: "ops" }] } }); return true;
    }
    return false;
  });
  await page.getByLabel("历史节点 ID", { exact: true }).fill(node.node_id);
  await page.getByRole("button", { name: "查询指令历史", exact: true }).click();
  await page.getByRole("button", { name: "查看此指令回执", exact: true }).waitFor();
  await page.getByRole("button", { name: "加载更早指令", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "历史范围不匹配" }).waitFor();
  assert.equal(await page.getByRole("button", { name: "查看此指令回执", exact: true }).count(), 0);
  assert.equal(calls.filter(row => row.path.endsWith("/command-history"))[1].url.searchParams.get("before_id"), "10");
  assert.equal(writes().length, 0);
}));

test("节点组件：历史重复指令与非法状态拒收，不保留旧页或下发", async () => mount(engine, "nodes", async ({ page, writes, handle }) => {
  let invalid = false;
  handle(async (route, row) => {
    if (!row.path.endsWith("/command-history")) return false;
    await route.fulfill({ json: { tenant_id: "default", node_id: node.node_id, order: "newest_first", next_before_id: invalid ? null : 10,
      items: [{ ...receipt(invalid ? "forged-state" : "pending"), created_by: "ops" }] } });
    return true;
  });
  await page.getByLabel("历史节点 ID", { exact: true }).fill(node.node_id);
  await page.getByRole("button", { name: "查询指令历史", exact: true }).click();
  await page.getByRole("button", { name: "查看此指令回执", exact: true }).waitFor();
  invalid = true;
  await page.getByRole("button", { name: "加载更早指令", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "历史记录无效" }).waitFor();
  assert.equal(await page.getByRole("button", { name: "查看此指令回执", exact: true }).count(), 0);
  assert.equal(writes().length, 0);
}));

test("节点组件：模型 CAS、签名 Skill 引用与优雅重启表单只提交约定参数", async () => mount(engine, "nodes", async ({ page, writes, handle }) => {
  let kind;
  handle(async (route, row) => {
    if (row.method === "POST") { await route.fulfill({ status: 202, json: { id: "synthetic-command" } }); return true; }
    if (row.path.endsWith("/commands/synthetic-command")) { await route.fulfill({ json: { ...receipt(), type: kind } }); return true; }
    return false;
  });
  const form = page.getByRole("group", { name: "固定指令类型与结构化参数", exact: true });
  await form.getByRole("combobox", { name: /^目标节点/ }).selectOption(node.node_id);
  const cases = [
    { type: "switch_model", fields: { "Agent ID": "synthetic-agent", "基线版本": "1", "预期当前版本": "2", "目标版本": "3", "目标模型别名": "mock-next" },
      args: { agent_id: "synthetic-agent", base_version: "1", expected_version: "2", target_version: "3", model_alias: "mock-next" } },
    { type: "reload_skills", fields: { "Skill 名称": "synthetic-skill", "Skill 版本": "1", "清单 SHA256": "a".repeat(64) },
      args: { skills: [{ name: "synthetic-skill", version: "1", manifest_sha256: "a".repeat(64) }] } },
    { type: "graceful_restart", fields: { "排空超时秒数": "15" }, args: { timeout_s: 15 } },
  ];
  for (const scenario of cases) {
    kind = scenario.type;
    await form.getByRole("combobox", { name: /^指令类型/ }).selectOption(kind);
    for (const [label, value] of Object.entries(scenario.fields)) await form.getByLabel(label, { exact: true }).fill(value);
    const before = writes().length;
    await page.getByRole("button", { name: "校验并预览指令", exact: true }).click();
    assert.equal(writes().length, before);
    await page.getByRole("button", { name: "确认下发一次", exact: true }).click();
    await page.getByText(`${kind} / pending`, { exact: true }).waitFor();
    assert.equal(writes().length, before + 1);
    assert.deepEqual(writes().at(-1).body, { type: kind, args: scenario.args });
  }
}));


test("F7 回执诊断不把重启 ok 当作完成，错误与身份切换清空且只读", async () => mount(engine, "nodes", async ({ page, writes, handle }) => {
  let status = "ok";
  handle(async (route, row) => {
    if (!row.path.endsWith("/commands/synthetic-command")) return false;
    await route.fulfill({ json: { ...receipt(status), type: "graceful_restart" } }); return true;
  });
  await page.getByLabel("节点 ID", { exact: true }).fill(node.node_id);
  await page.getByLabel("指令 ID", { exact: true }).fill("synthetic-command");
  await page.getByRole("button", { name: "查询回执", exact: true }).click();
  const guidance = page.getByRole("complementary", { name: "回执诊断与下一步" });
  await guidance.waitFor();
  assert.match(await guidance.innerText(), /尚不能证明重启完成/);
  assert.match(await guidance.innerText(), /PID、boot_id/);
  status = "error";
  await page.getByRole("button", { name: "查询回执", exact: true }).click();
  await guidance.getByText(/执行错误/).waitFor();
  assert.doesNotMatch(await guidance.innerText(), /尚不能证明重启完成/);
  await page.getByLabel("租户", { exact: true }).fill("other");
  await page.getByLabel("租户", { exact: true }).blur();
  assert.equal(await guidance.count(), 0);
  assert.equal(writes().length, 0);
}));
