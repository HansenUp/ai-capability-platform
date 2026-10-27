import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import { browser } from "./support.mjs";
import { held, hold, releaseHeld } from "./readonly-fixture.mjs";
import { dialog, directoryIdentity, mountDirectorySkills, reviewer, skill, skillIdentity, skillRow, token, tokenField, user } from "./directory-skills-fixture.mjs";
let engine;
before(async () => { engine = await browser(); });
after(async () => { await engine?.close(); });

test("Directory：租户展示与用户映射精确表单，编辑身份不可变、清空不写入", async () => mountDirectorySkills(engine, "directory", async ({ page, writes, handle }) => {
  handle(async (route, row) => { if (row.method === "GET") return false; await route.fulfill({ status: row.method === "POST" ? 201 : 200, json: {} }); return true; });
  await page.getByLabel("租户显示名称", { exact: true }).fill("仅展示元数据");
  await page.getByRole("button", { name: "保存租户元数据", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "未创建或修改真实租户" }).waitFor();
  assert.deepEqual([writes()[0].path, writes()[0].method, writes()[0].body], ["/directory/tenant", "PUT", { display_name: "仅展示元数据", description: "synthetic tenant metadata" }]);
  for (const [name, value] of Object.entries({ "IdP issuer": "https://idp.test/new", "IdP subject": "synthetic-new", "用户显示名称": "合成新映射", "用户说明": "metadata-only" })) await page.getByLabel(name, { exact: true }).fill(value);
  await page.getByRole("button", { name: "新增映射", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "目录映射已保存" }).waitFor();
  assert.deepEqual(writes()[1].body, { issuer: "https://idp.test/new", subject: "synthetic-new", display_name: "合成新映射", description: "metadata-only" });
  await page.getByRole("button", { name: "编辑", exact: true }).click();
  assert.equal(await page.getByLabel("IdP issuer", { exact: true }).isDisabled(), true);
  assert.equal(await page.getByLabel("IdP subject", { exact: true }).isDisabled(), true);
  await page.getByLabel("用户显示名称", { exact: true }).fill("修改展示名");
  await page.getByRole("button", { name: "保存展示信息", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "目录映射已保存" }).waitFor();
  assert.deepEqual([writes()[2].path, writes()[2].method, writes()[2].body], ["/directory/users/1", "PUT", { display_name: "修改展示名", description: user.description }]);
  await page.getByRole("button", { name: "编辑", exact: true }).click();
  await page.getByRole("button", { name: "清空编辑", exact: true }).click();
  assert.equal(await page.getByLabel("IdP subject", { exact: true }).inputValue(), "");
  assert.equal(writes().length, 3);
  assert.equal(writes().every(row => row.headers["x-console-tenant"] === "default" && !Object.hasOwn(row.body, "roles")), true);
}));

test("Directory：删除映射确认与取消，不调用账号或撤权接口", async () => mountDirectorySkills(engine, "directory", async ({ page, writes, handle }) => {
  let removed = false;
  handle(async (route, row) => {
    if (row.method === "DELETE") { removed = true; await route.fulfill({ status: 204 }); return true; }
    if (removed && row.path === "/directory/users") { await route.fulfill({ json: { items: [], next_after_id: null, scope: "display_metadata_only" } }); return true; }
    return false;
  });
  await page.getByRole("button", { name: "删除映射", exact: true }).click();
  await page.getByRole("button", { name: "取消删除", exact: true }).click(); assert.equal(writes().length, 0);
  await page.getByRole("button", { name: "删除映射", exact: true }).click();
  assert.equal(writes().length, 0);
  await page.getByRole("button", { name: "确认仅删除映射", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "账号、Agent 分配、令牌与 Run 均未改变" }).waitFor();
  assert.deepEqual(writes().map(row => [row.method, row.path]), [["DELETE", "/directory/users/1"]]);
  assert.equal(await page.getByRole("button", { name: "删除映射", exact: true }).count(), 0);
}));

test("Directory：拒绝错误不回显正文，身份切换清空表单与确认，USER 无写权限", async () => mountDirectorySkills(engine, "directory", async ({ page, calls, writes, handle }) => {
  handle(async (route, row) => { if (row.headers["x-console-role"] === "USER" || row.method !== "GET") { await route.fulfill({ status: 403, body: "synthetic-private-directory-body" }); return true; } return false; });
  await page.getByRole("button", { name: "保存租户元数据", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "403" }).waitFor();
  assert.equal((await page.locator("body").innerText()).includes("synthetic-private-directory-body"), false);
  assert.equal(await page.getByRole("button", { name: "编辑", exact: true }).count(), 0);
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await page.getByRole("button", { name: "编辑", exact: true }).click();
  await page.getByRole("button", { name: "删除映射", exact: true }).click();
  await directoryIdentity(page, "other", "USER");
  await page.getByRole("status").filter({ hasText: "403" }).waitFor();
  assert.equal(await page.getByRole("button", { name: "确认仅删除映射", exact: true }).count(), 0);
  assert.equal(await page.getByLabel("IdP subject", { exact: true }).inputValue(), "");
  assert.equal(await page.getByRole("button", { name: "保存租户元数据", exact: true }).isDisabled(), true);
  assert.equal(await page.getByRole("button", { name: "新增映射", exact: true }).isDisabled(), true);
  assert.equal(calls.at(-1).headers["x-console-tenant"], "other"); assert.equal(writes().length, 1);
}));

test("Directory：分页游标与租户范围校验，跨租户结果失败关闭", async () => mountDirectorySkills(engine, "directory", async ({ page, calls, handle }) => {
  let foreign = false;
  handle(async (route, row) => {
    if (row.path === "/directory/users") {
      const more = row.url.searchParams.has("after_id");
      await route.fulfill({ json: { scope: "display_metadata_only", items: [{ ...user, id: more ? 2 : 1, subject: more ? "synthetic-page-2" : user.subject, tenant_id: foreign ? "foreign" : "default" }], next_after_id: more ? null : 1 } }); return true;
    }
    return false;
  });
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await page.getByRole("button", { name: "加载更多", exact: true }).click();
  await page.getByRole("row").filter({ hasText: "synthetic-page-2" }).waitFor();
  assert.equal(calls.find(row => row.url.searchParams.has("after_id")).url.searchParams.get("after_id"), "1");
  foreign = true;
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "目录范围不匹配" }).waitFor();
  assert.equal(await page.getByRole("button", { name: "编辑", exact: true }).count(), 0);
}));

test("Directory：忙时锁身份，卸载后迟到正文不能继续读取或污染新实例", async () => mountDirectorySkills(engine, "directory", async ({ page, calls }) => {
  await hold(page); await page.getByRole("button", { name: "刷新", exact: true }).click(); await held(page);
  assert.equal(await page.getByRole("button", { name: "应用开发身份", exact: true }).isDisabled(), true);
  await page.locator("#unmount").click();
  await page.evaluate(() => { window.syntheticTransport.hold = false; });
  await page.locator("#remount").click();
  await page.getByRole("button", { name: "编辑", exact: true }).waitFor();
  await releaseHeld(page);
  assert.equal(await page.evaluate(() => window.syntheticTransport.paths.filter(path => path === "/console/directory/users").length), 2);
  assert.equal(calls.filter(row => row.path === "/directory/users").length, 2, "Only initial and newly mounted directory may load users");
}));

test("Skills：状态查询、编码定位与扫描评测详情，无变更请求", async () => mountDirectorySkills(engine, "skills", async ({ page, calls, writes, handle }) => {
  await skillRow(page).getByRole("button", { name: "详情", exact: true }).click();
  await page.getByText(/synthetic finding/).waitFor();
  assert.match(await page.locator("body").innerText(), /synthetic-eval/);
  await page.getByRole("combobox").nth(1).selectOption("evaluated"); await skillRow(page).waitFor();
  assert.equal(calls.at(-1).url.searchParams.get("status"), "evaluated");
  handle(async (route, row) => { if (row.path.includes("/versions/")) { await route.fulfill({ json: { ...skill, name: "synthetic space", version: "v/1" } }); return true; } return false; });
  await page.getByPlaceholder("Skill 名称", { exact: true }).fill("synthetic space");
  await page.getByPlaceholder("版本", { exact: true }).fill("v/1");
  await page.getByRole("button", { name: "查询", exact: true }).click();
  await page.getByRole("row").filter({ hasText: "synthetic space@v/1" }).waitFor();
  assert.equal(calls.at(-1).path, "/skills/synthetic%20space/versions/v%2F1"); assert.equal(writes().length, 0);
}));

test("Skills：审批需用户令牌，确认取消不写，自批／角色由网关 403 最终拒绝", async () => mountDirectorySkills(engine, "skills", async ({ page, writes, handle }) => {
  assert.equal(await skillRow(page).getByRole("button", { name: "审批通过", exact: true }).isDisabled(), true);
  await skillIdentity(page).getByRole("combobox").selectOption("USER");
  await reviewer(page);
  await dialog(page, skillRow(page).getByRole("button", { name: "审批通过", exact: true }), null); assert.equal(writes().length, 0);
  handle(async (route, row) => { if (row.method === "POST") { await route.fulfill({ status: 403, body: "synthetic-private-gateway-body" }); return true; } return false; });
  await dialog(page, skillRow(page).getByRole("button", { name: "审批通过", exact: true }), "");
  await page.getByText(/→ 403/).waitFor();
  assert.equal((await page.locator("body").innerText()).includes("synthetic-private-gateway-body"), false);
  assert.deepEqual([writes()[0].path, writes()[0].body], ["/skills/synthetic-skill/versions/1/approve", {}]);
  assert.equal(writes()[0].headers["x-ai-user-token"], token); assert.equal(writes().length, 1);
}));

test("Skills：审批确认后只提交一次，忙时锁身份与令牌，成功按原版本刷新", async () => mountDirectorySkills(engine, "skills", async ({ page, writes, handle }) => {
  await reviewer(page);
  let listed = false;
  handle(async (route, row) => {
    if (row.method === "POST") { listed = true; await route.fulfill({ json: {} }); return true; }
    if (listed && row.path.includes("/versions/")) { await route.fulfill({ json: { ...skill, status: "listed" } }); return true; }
    return false;
  });
  await hold(page);
  await dialog(page, skillRow(page).getByRole("button", { name: "审批通过", exact: true }), ""); await held(page);
  assert.equal(await skillIdentity(page).getByRole("combobox").isDisabled(), true);
  assert.equal(await tokenField(page).isDisabled(), true);
  assert.equal(await skillRow(page).getByRole("button", { name: "审批通过", exact: true }).isDisabled(), true);
  await releaseHeld(page);
  await skillRow(page).getByRole("button", { name: "吊销", exact: true }).waitFor(); assert.equal(writes().length, 1);
}));

test("Skills：吊销角色与原因校验，取消无请求，确认仅传原因", async () => mountDirectorySkills(engine, "skills", async ({ page, writes, handle }) => {
  handle(async (route, row) => {
    if (row.method === "GET") { await route.fulfill({ json: row.path === "/skills" ? [{ ...skill, status: "listed" }] : { ...skill, status: "revoked" } }); return true; }
    await route.fulfill({ json: {} }); return true;
  });
  await reviewer(page);
  await dialog(page, skillRow(page).getByRole("button", { name: "吊销", exact: true }), null); assert.equal(writes().length, 0);
  await dialog(page, skillRow(page).getByRole("button", { name: "吊销", exact: true }), "   ");
  await page.getByText(/吊销原因/).waitFor(); assert.equal(writes().length, 0);
  await dialog(page, skillRow(page).getByRole("button", { name: "吊销", exact: true }), "synthetic revoke reason");
  await skillRow(page).getByText("revoked", { exact: true }).waitFor();
  assert.deepEqual(writes()[0].body, { reason: "synthetic revoke reason" }); assert.equal(writes().length, 1);
  await skillIdentity(page).getByRole("combobox").selectOption("USER");
  await reviewer(page);
  assert.equal(await skillRow(page).getByRole("button", { name: "吊销", exact: true }).isDisabled(), true);
}));

test("Skills：切换操作者清空 reviewer 令牌、详情与查询，不将旧令牌带到新租户", async () => mountDirectorySkills(engine, "skills", async ({ page, calls, handle }) => {
  await reviewer(page); await skillRow(page).getByRole("button", { name: "详情", exact: true }).click();
  await page.getByPlaceholder("Skill 名称", { exact: true }).fill("old-lookup");
  handle(async (route, row) => { if (row.headers["x-console-tenant"] === "other") { await route.fulfill({ json: [] }); return true; } return false; });
  const refreshed = page.waitForResponse(response => response.request().headers()["x-console-tenant"] === "other");
  const tenant = skillIdentity(page).locator("input").nth(1); await tenant.fill("other"); await tenant.blur(); await refreshed;
  assert.equal(await tokenField(page).inputValue(), "");
  assert.equal(await page.getByPlaceholder("Skill 名称", { exact: true }).inputValue(), "");
  assert.equal(calls.at(-1).headers["x-ai-user-token"], undefined);
  assert.equal(await page.getByText(/synthetic finding/).count(), 0);
}));

test("Skills：旧筛选响应迟到不覆盖新结果，HTTP 与网络错误清空并受控展示", async () => mountDirectorySkills(engine, "skills", async ({ page, handle }) => {
  let failure = "";
  handle(async (route, row) => {
    if (failure === "http") await route.fulfill({ status: 503, body: "synthetic-private-list-body" });
    else if (failure === "network") await route.abort("failed");
    else await route.fulfill({ json: row.url.searchParams.get("status") === "listed" ? [{ ...skill, name: "next-skill", status: "listed" }] : [skill] });
    return true;
  });
  await hold(page); await page.getByRole("button", { name: "刷新", exact: true }).click(); await held(page);
  await page.evaluate(() => { window.syntheticTransport.hold = false; });
  await page.getByRole("combobox").nth(1).selectOption("listed");
  await page.getByRole("row").filter({ hasText: "next-skill@1" }).waitFor();
  await releaseHeld(page);
  assert.equal(await skillRow(page).count(), 0);
  failure = "http"; await page.getByRole("button", { name: "刷新", exact: true }).click();
  await page.getByText(/→ 503/).waitFor();
  assert.equal((await page.locator("body").innerText()).includes("synthetic-private-list-body"), false);
  assert.equal(await page.getByRole("row").filter({ hasText: "next-skill@1" }).count(), 0);
  failure = "network"; await page.getByRole("button", { name: "刷新", exact: true }).click();
  await page.getByText(/请求失败/).waitFor();
}));
