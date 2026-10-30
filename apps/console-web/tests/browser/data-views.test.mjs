import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { apply, phase, browser, profile } from "./support.mjs";
test("F4：合成视图请求预览、遮盖说明、校验与变更清空，不执行查询", async () => {
  const engine = await browser(), context = await engine.newContext(), page = await context.newPage();
  page.setDefaultTimeout(5000);
  const calls = [];
  await page.route("**/console/**", async route => { calls.push(route.request().url()); await route.abort(); });
  await page.route("**/v1/**", async route => { calls.push(route.request().url()); await route.abort(); });
  try {
    await page.goto(profile.url + "/manage/data-views");
    await page.getByRole("heading", { name: "受限视图 · 合成请求预览", exact: true }).waitFor();
    const guide = page.getByRole("region", { name: "F4统一体验指引", exact: true });
    assert.match(await guide.innerText(), /清单版本（不是策略版本）/);
    for (const [label, target] of [["1. 清单注册", "#f4-registration"], ["2. 策略授权", "#f4-policy"], ["3. 受益人查询", "#f4-query"]]) {
      assert.equal(await guide.getByRole("link", { name: label, exact: true }).getAttribute("href"), target);
      assert.equal(await page.locator(target).count(), 1);
    }
    await guide.getByText("失败或结果未知时如何处理", { exact: true }).click();
    assert.match(await guide.innerText(), /Run完成不是查询成功/);

    await page.getByLabel("secret遮盖投影", { exact: true }).check();
    await page.getByLabel("item等值条件（留空不筛选）", { exact: true }).fill("a1' OR 1=1");
    const build = page.getByRole("button", { name: "生成受限请求预览", exact: true });
    await build.click();
    const result = page.getByRole("region", { name: "受限请求草稿", exact: true });
    const request = JSON.parse(await result.locator("pre").innerText());
    assert.deepEqual(request, { view_id: "orders", version: "v1", columns: ["item", "secret"], filters: [{ column: "item", value: "a1' OR 1=1" }], limit: 10 });
    await page.getByLabel("最多返回行数", { exact: true }).fill("101");
    assert.equal(await result.count(), 0);
    await build.click(); await page.getByRole("alert").waitFor();
    await page.getByLabel("最多返回行数", { exact: true }).fill("1");
    await page.getByLabel("item投影", { exact: true }).uncheck();
    await page.getByLabel("secret遮盖投影", { exact: true }).uncheck();
    await build.click(); await page.getByRole("alert").waitFor();
    await page.reload();
    assert.equal(await result.count(), 0);
    assert.deepEqual(calls, []);
    assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
  } finally { await context.close(); await engine.close(); }
});

test("F4：签名身份登记独立查询、遮盖结果、本人ACK及身份隔离", async () => {
  const tokens = JSON.parse(await readFile(profile.tokens, "utf8"));
  const engine = await browser(), context = await engine.newContext(), page = await context.newPage();
  page.setDefaultTimeout(5000);
  try {
    await page.goto(profile.url + "/manage/data-views");
    await page.getByLabel("secret遮盖投影", { exact: true }).check();
    await page.getByLabel("最多返回行数", { exact: true }).fill("1");
    await page.getByRole("button", { name: "生成受限请求预览", exact: true }).click();
    await page.getByRole("button", { name: "选择草稿进入隔离查询", exact: true }).click();
    await apply(page, tokens.alice);
    await page.getByRole("button", { name: "只读核对写账本", exact: true }).click();
    await page.getByRole("button", { name: "填入受限查询草稿（不创建）", exact: true }).click();
    assert.equal(await page.getByRole("region", { name: "隔离会话", exact: true }).count(), 0);
    const preparedResponse = page.waitForResponse(r => r.url().endsWith("/v1/workbench/create-attempts") && r.request().method() === "POST");
    await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).click();
    const prepared = await (await preparedResponse).json();
    assert.equal(prepared.status, "PREPARED");
    await page.getByRole("button", { name: "确认执行已登记创建一次", exact: true }).click();
    await page.getByRole("button", { name: "预览本人确认收口", exact: true }).waitFor();
    await page.getByLabel("Run ID", { exact: true }).fill(prepared.run_id);
    try { await phase(page, prepared.run_id, "completed", tokens.alice); }
    catch (error) { const events = await (await page.request.get(`${profile.runtime}/v1/runs/${prepared.run_id}/events`, { headers: { Authorization: `Bearer ${tokens.alice}` } })).json(); console.log("F4_FAILURE_DIAGNOSTIC", JSON.stringify(events.filter(e => /failed|error|decision/.test(e.type)))); throw error; }
    const table = page.getByRole("region", { name: "受限查询结果", exact: true });
    await table.waitFor();
    assert.match(await table.innerText(), /REDACTED/);
    assert.match(await table.innerText(), /已截断/);
    assert.equal((await page.locator("body").innerText()).includes("NEVER-RETURN"), false);
    assert.equal((await page.locator("body").innerText()).includes("foreign-row"), false);
    assert.equal(await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), true);
    for (const token of [tokens.bob, tokens.foreign]) {
      for (const suffix of ["", "/events", "/stream"]) {
        const response = await page.request.get(`${profile.runtime}/v1/runs/${prepared.run_id}${suffix}`, { headers: { Authorization: `Bearer ${token}` } });
        assert.equal(response.status(), token === tokens.foreign ? 404 : 403);
      }
    }
    await page.getByRole("button", { name: "预览本人确认收口", exact: true }).click();
    await page.getByRole("button", { name: "确认收口此回执", exact: true }).click();
    await page.waitForFunction(() => { const button = [...document.querySelectorAll("button")].find(b => b.textContent?.trim() === "登记新 Run（尚未创建）"); return !!button && !button.matches(":disabled"); });
    await apply(page, tokens.bob);
    assert.equal(await table.count(), 0);
    await page.getByRole("button", { name: "只读核对写账本", exact: true }).click();
    await page.getByRole("button", { name: "填入受限查询草稿（不创建）", exact: true }).click();
    const deniedPreparedResponse = page.waitForResponse(r => r.url().endsWith("/v1/workbench/create-attempts") && r.request().method() === "POST");
    await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).click();
    const deniedPrepared = await (await deniedPreparedResponse).json();
    assert.notEqual(deniedPrepared.run_id, prepared.run_id);
    await page.getByRole("button", { name: "确认执行已登记创建一次", exact: true }).click();
    await page.getByRole("button", { name: "预览本人确认收口", exact: true }).waitFor();
    await page.getByLabel("Run ID", { exact: true }).fill(deniedPrepared.run_id);
    await phase(page, deniedPrepared.run_id, "completed", tokens.bob); // Run completion is not tool success.
    assert.equal(await table.count(), 0);
    const deniedEvents = await (await page.request.get(`${profile.runtime}/v1/runs/${deniedPrepared.run_id}/events`, { headers: { Authorization: `Bearer ${tokens.bob}` } })).json();
    assert.equal(deniedEvents.some(e => e.type === "tool.result" && e.payload.status === "ok"), false);
    assert.equal(deniedEvents.filter(e => e.type === "tool.result" && e.payload.status === "error").length, 1);
    assert.equal(await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), true);
    await page.getByRole("button", { name: "预览本人确认收口", exact: true }).click();
    await page.getByRole("button", { name: "确认收口此回执", exact: true }).click();
    await page.waitForFunction(() => { const button = [...document.querySelectorAll("button")].find(b => b.textContent?.trim() === "登记新 Run（尚未创建）"); return !!button && !button.matches(":disabled"); });
    const settled = await (await page.request.get(`${profile.runtime}/v1/workbench/attempts`, { headers: { Authorization: `Bearer ${tokens.bob}` } })).json();
    assert.equal(settled.active, null);
  } finally { await context.close(); await engine.close(); }
});

test("F4注册提案：差异、校验、固定遮盖及重载清空，不改变查询或权限", async () => {
  const engine = await browser(), context = await engine.newContext(), page = await context.newPage();
  page.setDefaultTimeout(5000);
  const requests = [];
  for (const pattern of ["**/console/**", "**/v1/**"]) await page.route(pattern, async route => { requests.push(route.request().url()); await route.abort(); });
  try {
    await page.goto(profile.url + "/manage/data-views");
    const draft = page.getByRole("region", { name: "受限视图注册草稿", exact: true });
    const generate = draft.getByRole("button", { name: "校验并生成注册提案", exact: true });
    const preview = draft.getByRole("region", { name: "本机注册提案", exact: true });
    await generate.click();
    const value = JSON.parse(await preview.locator("pre").innerText());
    assert.deepEqual(value, { schema: "synthetic-view-proposal/v1", status: "local-draft-not-authorized", view_id: "orders", based_on_version: "v1", proposed_version: "v2", columns: ["item", "secret"], masked_columns: ["secret"], filter_columns: ["item"], max_rows: 100 });
    await draft.getByLabel("草稿版本", { exact: true }).fill("v1");
    assert.equal(await preview.count(), 0); await generate.click(); await draft.getByRole("alert").waitFor();
    await draft.getByLabel("草稿版本", { exact: true }).fill("v3");
    await draft.getByLabel("草稿限行", { exact: true }).fill("101");
    await generate.click(); await draft.getByRole("alert").waitFor();
    await draft.getByLabel("草稿限行", { exact: true }).fill("10");
    await draft.getByLabel("草稿item投影", { exact: true }).uncheck();
    await generate.click(); await draft.getByRole("alert").waitFor();
    await draft.getByLabel("草稿允许item等值过滤", { exact: true }).uncheck();
    await generate.click();
    assert.deepEqual(JSON.parse(await preview.locator("pre").innerText()).masked_columns, ["secret"]);
    assert.match(await draft.getByLabel("注册变更对比").innerText(), /v1 → v3/);
    await page.getByRole("button", { name: "生成受限请求预览", exact: true }).click();
    assert.equal(JSON.parse(await page.getByRole("region", { name: "受限请求草稿", exact: true }).locator("pre").innerText()).version, "v1");
    await draft.getByRole("button", { name: "清空注册提案", exact: true }).click();
    assert.equal(await preview.count(), 0);
    await generate.click(); await page.reload();
    assert.equal(await preview.count(), 0);
    assert.deepEqual(requests, []);
    assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
  } finally { await context.close(); await engine.close(); }
});

test("F4注册管理：维护者提交、双人审核、显式发布丢响应恢复与本人ACK", async () => {
  const tokens = JSON.parse(await readFile(profile.tokens, "utf8"));
  const engine = await browser(), context = await engine.newContext(), page = await context.newPage();
  page.setDefaultTimeout(5000);
  const version = "browser-" + crypto.randomUUID();
  const panel = page.getByRole("region", { name: "隔离注册管理", exact: true });
  const restore = panel.getByRole("button", { name: "只读恢复注册账本与清单", exact: true });
  const receipt = panel.getByRole("region", { name: "注册操作回执", exact: true });
  async function identity(token) {
    await panel.getByLabel("注册管理用户令牌", { exact: true }).fill(token);
    await panel.getByRole("button", { name: "应用注册身份并清空", exact: true }).click();
    await panel.getByRole("status").filter({ hasText: "已只读核对主体账本" }).waitFor();
    await page.waitForFunction(() => ![...document.querySelectorAll("button")].find(b => b.textContent === "只读恢复注册账本与清单")?.disabled);
  }
  async function read() { await restore.click(); await page.waitForFunction(() => ![...document.querySelectorAll("button")].find(b => b.textContent === "只读恢复注册账本与清单")?.disabled); }
  async function acknowledge() {
    await receipt.getByRole("button", { name: "预览注册本人收口", exact: true }).click();
    await receipt.getByRole("button", { name: "确认注册本人收口", exact: true }).click();
    await read(); assert.equal(await receipt.count(), 0);
  }
  async function step(action) {
    if (action !== "draft") await panel.getByRole("button", { name: new RegExp("^" + version + " ·") }).click();
    await panel.getByRole("button", { name: action === "draft" ? "登记创建注册草稿" : "登记 " + action, exact: true }).click();
    await read(); await receipt.getByText(/PREPARED/).first().waitFor();
    await receipt.getByRole("button", { name: "执行已登记注册操作一次", exact: true }).click();
    await read(); await receipt.getByText(/COMMITTED/).first().waitFor(); await acknowledge();
  }
  try {
    await page.goto(profile.url + "/manage/data-views");
    await identity(tokens.view_maintainer);
    await panel.getByLabel("注册新版本", { exact: true }).fill(version);
    await panel.getByLabel("注册允许item等值过滤", { exact: true }).uncheck();
    await step("draft"); await step("submit");
    await panel.getByRole("button", { name: new RegExp("^" + version + " ·") }).click();
    assert.equal(await panel.getByRole("button", { name: "登记 approve", exact: true }).isDisabled(), true);
    assert.equal(await panel.getByRole("button", { name: "登记 publish", exact: true }).isDisabled(), true);
    await identity(tokens.view_reviewer_a); await step("approve");
    await identity(tokens.view_reviewer_b); await step("approve");
    await identity(tokens.view_maintainer);
    await panel.getByRole("button", { name: new RegExp("^" + version + " ·") }).click();
    await panel.getByRole("button", { name: "登记 publish", exact: true }).click(); await read();
    let committed;
    await page.route("**/v1/isolated-views/*/execute", async route => {
      const response = await route.fetch(); assert.equal(response.status(), 200); committed = await response.json(); await route.abort("failed");
    }, { times: 1 });
    await receipt.getByRole("button", { name: "执行已登记注册操作一次", exact: true }).click();
    await panel.getByRole("status").filter({ hasText: "结果未知" }).waitFor();
    assert.equal(await panel.getByRole("button", { name: "登记创建注册草稿", exact: true }).isDisabled(), true);
    await page.reload(); await identity(tokens.view_reviewer_a); assert.equal(await receipt.count(), 0);
    await identity(tokens.view_maintainer); await receipt.getByText(/COMMITTED/).first().waitFor();
    assert.equal(committed.evidence.snapshot.state, "active");
    assert.equal(committed.evidence.snapshot.proposal.filter_item, false);
    assert.equal(await receipt.getByRole("button", { name: "执行已登记注册操作一次", exact: true }).count(), 0);
    await acknowledge(); await step("revoke");
    await panel.getByRole("button", { name: new RegExp("^" + version + " · revoked") }).waitFor();
  } finally { await context.close(); await engine.close(); }
});

test("F4策略：双审授权新清单、丢响应恢复、查询成功与撤权失败关闭", async () => {
  const tokens = JSON.parse(await readFile(profile.tokens, "utf8"));
  const engine = await browser(), context = await engine.newContext(), page = await context.newPage();
  page.setDefaultTimeout(5000);
  const version = "policy-view-" + crypto.randomUUID(), policyVersion = "permit-" + crypto.randomUUID();
  const panel = page.getByRole("region", { name: "隔离策略管理", exact: true });
  const receipt = panel.getByRole("region", { name: "策略操作回执", exact: true });
  async function api(action, token, target = null, proposal = null) {
    const headers = { Authorization: `Bearer ${token}` };
    const preparedResponse = await page.request.post(profile.runtime + "/v1/isolated-views/prepare", { headers, data: { attempt_id: crypto.randomUUID(), action, registration_id: target?.id ?? null, expected_revision: target?.revision ?? 0, proposal } });
    assert.equal(preparedResponse.status(), 200); const prepared = await preparedResponse.json();
    const executed = await page.request.post(`${profile.runtime}/v1/isolated-views/${prepared.attempt_id}/execute`, { headers, data: { fingerprint: prepared.fingerprint, revision: prepared.revision } });
    assert.equal(executed.status(), 200); const done = await executed.json(); assert.equal(done.status, "COMMITTED");
    const ack = await page.request.post(`${profile.runtime}/v1/isolated-views/${done.attempt_id}/acknowledge`, { headers, data: { fingerprint: done.fingerprint, revision: done.revision } });
    assert.equal(ack.status(), 200); return done.evidence.snapshot;
  }
  async function idle() { await page.waitForFunction(() => { const b = [...document.querySelectorAll("button")].find(b => b.textContent === "只读恢复策略账本与清单"); return b && !b.matches(":disabled"); }); }
  async function identity(token) {
    await panel.getByLabel("策略管理用户令牌", { exact: true }).fill(token);
    await panel.getByRole("button", { name: "应用策略身份并清空", exact: true }).click();
    await panel.getByRole("status").filter({ hasText: "已只读核对主体账本" }).waitFor(); await idle();
  }
  async function read() { await panel.getByRole("button", { name: "只读恢复策略账本与清单", exact: true }).click(); await idle(); }
  async function ack() {
    await receipt.getByRole("button", { name: "预览策略本人收口", exact: true }).click();
    await receipt.getByRole("button", { name: "确认策略本人收口", exact: true }).click(); await read(); assert.equal(await receipt.count(), 0);
  }
  async function step(action, drop = false) {
    if (action !== "draft") await panel.getByRole("button", { name: new RegExp("^" + policyVersion + " ·") }).click();
    await panel.getByRole("button", { name: action === "draft" ? "登记创建策略草稿" : "登记 " + action, exact: true }).click();
    await read(); await receipt.getByText(/PREPARED/).first().waitFor();
    if (drop) await page.route("**/v1/isolated-policies/*/execute", async route => { const r = await route.fetch(); assert.equal((await r.json()).status, "COMMITTED"); await route.abort("failed"); }, { times: 1 });
    await receipt.getByRole("button", { name: "执行已登记策略操作一次", exact: true }).click();
    if (drop) {
      await panel.getByRole("status").filter({ hasText: "结果未知" }).waitFor();
      await page.reload(); await identity(tokens.policy_reviewer_a); assert.equal(await receipt.count(), 0);
      await identity(tokens.policy_maintainer);
    } else await read();
    await receipt.getByText(/COMMITTED/).first().waitFor(); await ack();
  }
  async function query(expectSuccess, token = tokens.alice, filter = "") {
    await page.getByLabel("视图版本", { exact: true }).fill(version);
    await page.getByLabel("item等值条件（留空不筛选）", { exact: true }).fill(filter);
    await page.getByLabel("最多返回行数", { exact: true }).fill("1");
    await page.getByRole("button", { name: "生成受限请求预览", exact: true }).click();
    await page.getByRole("button", { name: "选择草稿进入隔离查询", exact: true }).click();
    await apply(page, token);
    await page.getByRole("button", { name: "只读核对写账本", exact: true }).click();
    await page.getByRole("button", { name: "填入受限查询草稿（不创建）", exact: true }).click();
    const response = page.waitForResponse(r => r.url().endsWith("/v1/workbench/create-attempts") && r.request().method() === "POST");
    await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).click();
    const prepared = await (await response).json(); assert.equal(prepared.status, "PREPARED");
    await page.getByRole("button", { name: "确认执行已登记创建一次", exact: true }).click();
    await page.getByRole("button", { name: "预览本人确认收口", exact: true }).waitFor();
    await page.getByLabel("Run ID", { exact: true }).fill(prepared.run_id);
    await phase(page, prepared.run_id, "completed", token);
    const events = await (await page.request.get(`${profile.runtime}/v1/runs/${prepared.run_id}/events`, { headers: { Authorization: `Bearer ${token}` } })).json();
    assert.equal(events.filter(e => e.type === "tool.result" && e.payload.status === "ok").length, expectSuccess ? 1 : 0);
    const table = page.getByRole("region", { name: "受限查询结果", exact: true });
    if (expectSuccess) { await table.waitFor(); assert.match(await table.innerText(), /a1/); }
    else { assert.equal(await table.count(), 0); assert.equal(events.filter(e => e.type === "tool.result" && e.payload.status === "error").length, 1); }
    await page.getByRole("button", { name: "预览本人确认收口", exact: true }).click();
    await page.getByRole("button", { name: "确认收口此回执", exact: true }).click();
    await page.waitForFunction(() => { const b = [...document.querySelectorAll("button")].find(b => b.textContent?.trim() === "登记新 Run（尚未创建）"); return b && !b.matches(":disabled"); });
    assert.equal((await (await page.request.get(profile.runtime + "/v1/workbench/attempts", { headers: { Authorization: `Bearer ${token}` } })).json()).active, null);
  }
  try {
    let manifest = await api("draft", tokens.view_maintainer, null, { version, columns: ["item", "secret"], filter_item: true, limit: 10 });
    manifest = await api("submit", tokens.view_maintainer, manifest);
    manifest = await api("approve", tokens.view_reviewer_a, manifest);
    manifest = await api("approve", tokens.view_reviewer_b, manifest);
    manifest = await api("publish", tokens.view_maintainer, manifest);
    await page.goto(profile.url + "/manage/data-views");
    await query(false); // Publishing a manifest alone never grants read_data.
    await identity(tokens.policy_maintainer);
    await panel.getByLabel("绑定清单ID", { exact: true }).fill(manifest.id);
    await panel.getByLabel("策略新版本", { exact: true }).fill(policyVersion);
    await panel.getByLabel("策略限行", { exact: true }).fill("2");
    await panel.getByLabel("策略允许item等值过滤", { exact: true }).uncheck();
    await step("draft"); await step("submit");
    await identity(tokens.policy_reviewer_a); await step("approve");
    await identity(tokens.policy_reviewer_b); await step("approve");
    await identity(tokens.policy_maintainer); await step("publish", true);
    await query(true); await query(false, tokens.bob);
    await query(false, tokens.alice, "a1");
    await panel.getByRole("button", { name: new RegExp("^" + policyVersion + " ·") }).click();
    assert.equal(await panel.getByLabel("策略允许item等值过滤", { exact: true }).isChecked(), false);
    await step("revoke"); await query(false);
    assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
  } finally { await context.close(); await engine.close(); }
});
