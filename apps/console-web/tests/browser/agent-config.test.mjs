import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import { browser, profile } from "./support.mjs";
import { mount, version } from "./management-fixture.mjs";
let engine;
before(async () => { engine = await browser(); });
after(async () => { await engine?.close(); });
const jsonLabel = "完整 config JSON（高级编辑，保留扩展字段）";
const advanced = page => page.getByLabel(jsonLabel, { exact: true });
const config = async page => JSON.parse(await advanced(page).inputValue());
async function component(action) {
  const context = await engine.newContext(); const page = await context.newPage(); page.setDefaultTimeout(5000);
  const calls = [];
  for (const pattern of ["**/console/**", "**/v1/**"]) await context.route(pattern, async route => { calls.push(route.request().url()); await route.fulfill({ status: 404 }); });
  try {
    await page.goto(`${profile.url}/tests/browser/agent-config.html`);
    await advanced(page).waitFor(); await action(page);
    assert.deepEqual(calls, [], "Standalone form must not issue API requests");
    assert.equal(await page.locator("#model-output").textContent(), await advanced(page).inputValue());
  } finally { await context.close(); }
}
test("AgentConfig DOM：结构化输入同步父模型、去重保序、显式清空与扩展保留", async () => component(async page => {
  const original = await config(page);
  assert.equal(await page.getByLabel("工具清单", { exact: true }).inputValue(), "echo");
  await page.getByLabel("工具清单", { exact: true }).fill("echo, search\necho");
  assert.deepEqual(await config(page), { ...original, tools: ["echo", "search"] });
  await page.getByLabel("红线禁止工具", { exact: true }).fill("delete\nwrite, delete");
  assert.deepEqual((await config(page)).redlines, { ...original.redlines, deny_tools: ["delete", "write"] });
  await page.getByLabel("工具清单", { exact: true }).fill("");
  assert.deepEqual((await config(page)).tools, []);
  assert.deepEqual((await config(page)).custom, original.custom);
  assert.equal(await page.locator("#updates").innerText(), await page.locator("#inputs").innerText(), "Exactly one update per native input; no recursive v-model emissions");
}));
test("AgentConfig DOM：高级 JSON 更新字段，恢复先前自身输出也必须重新同步", async () => component(async page => {
  await page.getByLabel("工具清单", { exact: true }).fill("first-tool");
  const first = await advanced(page).inputValue();
  await advanced(page).fill(JSON.stringify({ tools: ["external-tool"], redlines: { deny_tools: ["external-deny"] }, skills: [] }));
  assert.equal(await page.getByLabel("工具清单", { exact: true }).inputValue(), "external-tool");
  await advanced(page).fill(first);
  assert.equal(await page.getByLabel("工具清单", { exact: true }).inputValue(), "first-tool");
  assert.equal(await page.getByLabel("红线禁止工具", { exact: true }).inputValue(), "write");
  assert.equal(await page.getByLabel("Skill 名称", { exact: true }).inputValue(), "synthetic-skill");
  assert.equal(await page.locator("#updates").innerText(), await page.locator("#inputs").innerText());
}));
test("AgentConfig DOM：Skill 增删改只更新目标引用，保留版本扩展与其他配置", async () => component(async page => {
  const original = await config(page);
  await page.getByLabel("Skill 版本", { exact: true }).fill("2");
  assert.deepEqual((await config(page)).skills, [{ ...original.skills[0], version: "2" }]);
  await page.getByRole("button", { name: "添加 Skill", exact: true }).click();
  await page.getByLabel("Skill 名称", { exact: true }).nth(1).fill("second-skill");
  await page.getByLabel("Skill 版本", { exact: true }).nth(1).fill("3");
  assert.deepEqual((await config(page)).skills[0].extension, { pinned: true });
  await page.getByRole("button", { name: "移除此 Skill", exact: true }).nth(1).click();
  assert.deepEqual(await config(page), { ...original, skills: [{ ...original.skills[0], version: "2" }] });
}));
test("AgentConfig DOM：错误 JSON 禁用结构化编辑，不兼容字段不静默丢弃，修正后恢复", async () => component(async page => {
  for (const invalid of ["broken JSON", "[]", "null", "1"]) {
    await advanced(page).fill(invalid);
    await page.getByRole("alert").filter({ hasText: "JSON 无效" }).waitFor();
    assert.equal(await page.getByLabel("工具清单", { exact: true }).isDisabled(), true);
    assert.equal(await advanced(page).inputValue(), invalid);
  }
  const incompatible = JSON.stringify({ redlines: [], skills: "keep-original", custom: { untouched: true } });
  await advanced(page).fill(incompatible);
  const count = await page.locator("#updates").innerText();
  await page.getByLabel("红线禁止工具", { exact: true }).fill("deny");
  await page.getByRole("alert").filter({ hasText: "已有 redlines 不是对象" }).waitFor();
  assert.equal(await advanced(page).inputValue(), incompatible);
  assert.equal(await page.locator("#updates").innerText(), count);
  assert.equal(await page.getByRole("button", { name: "添加 Skill", exact: true }).isDisabled(), true);
  await page.getByLabel("工具清单", { exact: true }).fill("echo");
  assert.equal((await config(page)).skills, "keep-original");
  await advanced(page).fill('{"tools":["fixed"],"redlines":{"deny_tools":["fixed-deny"]},"skills":[]}');
  assert.equal(await page.getByRole("alert").count(), 0);
  assert.equal(await page.getByLabel("红线禁止工具", { exact: true }).inputValue(), "fixed-deny");
  assert.equal(await page.getByRole("button", { name: "添加 Skill", exact: true }).isDisabled(), false);
}));
test("Agents DOM：配置错误不发草稿请求，修正后保存精确配置且不发布", async () => mount(engine, "agents", async ({ page, writes, handle }) => {
  handle(async (route, row) => { if (row.method === "POST") { await route.fulfill({ status: 201, json: {} }); return true; } return false; });
  await page.getByLabel("Agent ID", { exact: true }).fill("synthetic-config-agent");
  await advanced(page).fill("[]");
  await page.getByRole("button", { name: "仅保存草稿", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "config 必须是 JSON 对象" }).waitFor();
  assert.equal(writes().length, 0);
  const original = { tools: ["echo"], hooks: [{ name: "synthetic" }], redlines: { deny_tools: [], extra: false }, custom: { retained: null } };
  await advanced(page).fill(JSON.stringify(original));
  await page.getByLabel("工具清单", { exact: true }).fill("echo, search");
  await page.getByRole("button", { name: "仅保存草稿", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "草稿已保存" }).waitFor();
  assert.deepEqual(writes()[0].body.config, { ...original, tools: ["echo", "search"] });
  assert.deepEqual(writes().map(row => row.path), ["/agents"]);
}));

async function comparisonFixture(action) {
  await mount(engine, "agents", async fixture => {
    const first = { ...version, status: "published", config: { tools: ["echo", "search"], extension: { retained: true } } };
    const second = { ...first, version: "2", status: "retired", system_prompt: "<img src=x onerror=alert(1)>", model_alias: "mock-next", harness: "synthetic-harness",
      config: { tools: ["search", "echo"], extension: { retained: false }, "a/b~": null } };
    const identical = { ...first, version: "3", status: "draft", eval_run_ref: "synthetic-eval", canary: { percent: 0, allowlist: ["human:test"] },
      config: { extension: { retained: true }, tools: ["echo", "search"] } };
    fixture.handle(async (route, row) => { if (row.method === "GET" && row.path === "/agents") { await route.fulfill({ json: [first, second, identical, { ...first, agent_id: "other-agent", version: "9" }] }); return true; } return false; });
    await fixture.page.getByRole("button", { name: "刷新版本", exact: true }).click();
    await fixture.page.getByRole("row").filter({ hasText: "other-agent" }).waitFor();
    await action(fixture);
  });
}
const comparison = page => page.locator("section").filter({ has: page.getByRole("heading", { name: "版本配置对比（只读）", exact: true }) });
test("版本对比 DOM：展示完整配置差异与转义路径，数组保序，纯只读不影响草稿", async () => comparisonFixture(async ({ page, writes }) => {
  const draft = await advanced(page).inputValue();
  await page.getByLabel("对比 Agent", { exact: false }).selectOption(version.agent_id);
  await page.getByLabel("基准版本", { exact: false }).selectOption("1");
  await page.getByLabel("目标版本", { exact: false }).selectOption("2");
  const rows = comparison(page).locator("tbody tr");
  assert.deepEqual(await rows.locator("td:first-child").allTextContents(), ["/config/a~1b~0", "/config/extension/retained", "/config/tools", "/harness", "/model_alias", "/system_prompt"]);
  const extension = rows.filter({ hasText: "/config/extension/retained" });
  assert.deepEqual(await extension.locator("td pre").allTextContents(), ["true", "false"]);
  const missing = rows.filter({ hasText: "/config/a~1b~0" });
  assert.equal(await missing.locator("td").nth(1).innerText(), "（缺失）"); assert.equal(await missing.locator("td").nth(2).innerText(), "null");
  const tools = rows.filter({ hasText: "/config/tools" });
  assert.deepEqual(JSON.parse(await tools.locator("td").nth(1).innerText()), ["echo", "search"]);
  assert.deepEqual(JSON.parse(await tools.locator("td").nth(2).innerText()), ["search", "echo"]);
  assert.equal(await comparison(page).locator("img").count(), 0);
  assert.equal(await advanced(page).inputValue(), draft); assert.equal(writes().length, 0);
}));
test("版本对比 DOM：对象键序与发布元信息不构成差异，同 Agent 边界与身份切换清空", async () => comparisonFixture(async ({ page, writes, handle }) => {
  await page.getByLabel("对比 Agent", { exact: false }).selectOption(version.agent_id);
  assert.deepEqual(await page.getByLabel("目标版本", { exact: false }).locator("option").allTextContents(), ["请选择", "1", "2", "3"]);
  await page.getByLabel("基准版本", { exact: false }).selectOption("1");
  await page.getByLabel("目标版本", { exact: false }).selectOption("3");
  await comparison(page).getByText("配置一致", { exact: true }).waitFor();
  await page.getByLabel("对比 Agent", { exact: false }).selectOption("other-agent");
  assert.equal(await page.getByLabel("基准版本", { exact: false }).inputValue(), "");
  assert.equal(await page.getByLabel("目标版本", { exact: false }).inputValue(), "");
  assert.equal(await comparison(page).locator("tbody tr").count(), 0);
  handle(async (route, row) => { if (row.path === "/agents") { await route.fulfill({ json: [] }); return true; } return false; });
  await page.getByLabel("租户", { exact: true }).fill("other");
  const refreshed = page.waitForResponse(response => response.url().endsWith("/console/agents") && response.request().headers()["x-console-tenant"] === "other");
  await page.getByRole("button", { name: "应用开发身份", exact: true }).click(); await refreshed;
  assert.equal(await page.getByLabel("对比 Agent", { exact: false }).inputValue(), "");
  assert.equal(await comparison(page).locator("tbody tr").count(), 0); assert.equal(writes().length, 0);
}));
