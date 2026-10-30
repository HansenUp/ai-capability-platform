import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { apply, browser, phase, profile } from "./support.mjs";

test("F2持久会话：登记丢响应、重载、多标签一次执行、权威历史显式续聊", async () => {
  const tokens = JSON.parse(await readFile(profile.tokens, "utf8"));
  const engine = await browser(), context = await engine.newContext(), page = await context.newPage();
  page.setDefaultTimeout(5000);
  const writes = [], previews = [];
  context.on("request", request => {
    const path = new URL(request.url()).pathname;
    if (request.method() === "POST" && path === "/v1/workbench/attachment-preview") previews.push(path);
    else if (request.method() === "POST" && path.startsWith("/v1/")) writes.push(path);
  });
  try {
    await page.goto(`${profile.url}/tests/browser/workbench.html`); await apply(page, tokens.alice);
    const original = "CREATE-DURABLE-SYNTHETIC";
    let prepared;
    await page.getByLabel("Agent ID", { exact: true }).fill("s12-workbench");
    await page.getByRole("button", { name: "只读核对写账本", exact: true }).click();
    await page.getByRole("button", { name: "开始新会话（尚未写入）", exact: true }).click();
    const conversationId = await page.getByLabel("会话", { exact: true }).inputValue();
    await page.getByLabel("选择合成txt附件", { exact: true }).setInputFiles({ name: "synthetic.txt", mimeType: "text/plain", buffer: Buffer.from("F2-SYNTHETIC-ATTACHMENT") });
    await page.getByText("F2-SYNTHETIC-ATTACHMENT", { exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), true);
    await page.getByRole("button", { name: "扫描合成附件", exact: true }).click();
    await page.getByLabel("附件状态", { exact: true }).filter({ hasText: "合成扫描通过" }).waitFor();

    await page.route("**/v1/workbench/create-attempts", async route => {
      const response = await route.fetch(); assert.equal(response.status(), 200); prepared = await response.json();
      assert.equal(prepared.status, "PREPARED"); await route.abort("failed");
    });

    await page.getByLabel("消息", { exact: true }).fill(original);
    await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "结果未知" }).waitFor();
    assert.equal(await page.getByLabel("Run ID", { exact: true }).inputValue(), "");
    assert.equal(await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), true);
    assert.equal(writes.length, 1);
    assert.equal(previews.length, 1);
    const absent = await page.request.get(`${profile.runtime}/v1/runs/${prepared.run_id}`, { headers: { Authorization: `Bearer ${tokens.alice}` } });
    assert.equal(absent.status(), 404);
    await page.reload(); await apply(page, tokens.alice_fresh);
    await page.getByRole("button", { name: "确认执行已登记创建一次", exact: true }).waitFor();
    assert.equal(await page.getByLabel("登记时的原始创建消息", { exact: true }).inputValue(), "");
    assert.equal((await page.locator("body").innerText()).includes(original), false);
    await page.getByRole("button", { name: "只读核对登记附件摘要", exact: true }).click();
    await page.getByText(/synthetic.txt · .*合成扫描通过/).waitFor();
    assert.equal((await page.locator("body").innerText()).includes("F2-SYNTHETIC-ATTACHMENT"), false);

    await context.setOffline(true);
    await page.getByRole("button", { name: "只读核对写账本", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "读取失败" }).waitFor();
    await context.setOffline(false);
    await page.getByRole("button", { name: "只读核对写账本", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "已恢复服务端活动账本" }).waitFor();
    const path = `/v1/workbench/attempts/${prepared.attempt_id}`;
    for (const token of [tokens.bob, tokens.foreign]) {
      const denied = await page.request.get(`${profile.runtime}${path}`, { headers: { Authorization: `Bearer ${token}` } });
      assert.equal(denied.status(), 404);
    }
    const second = await context.newPage(); await second.goto(`${profile.url}/tests/browser/workbench.html`); await apply(second, tokens.alice_fresh);
    await second.getByRole("button", { name: "确认执行已登记创建一次", exact: true }).waitFor();
    let committed;
    await page.route(`**${path}/execute`, async route => {
      const response = await route.fetch(); assert.equal(response.status(), 200); committed = await response.json();
      assert.equal(committed.status, "COMMITTED"); await route.abort("failed");
    });
    for (const target of [page, second]) await target.getByLabel("登记时的原始创建消息", { exact: true }).fill(original);
    await Promise.all([page.getByRole("button", { name: "确认执行已登记创建一次", exact: true }).click(),
      second.getByRole("button", { name: "确认执行已登记创建一次", exact: true }).click()]);
    await page.getByRole("status").filter({ hasText: "结果未知" }).waitFor();
    await second.getByRole("button", { name: "预览本人确认收口", exact: true }).waitFor();
    assert.equal(committed.run_id, prepared.run_id);
    await page.reload(); await apply(page, tokens.alice_fresh);
    await page.getByRole("button", { name: "预览本人确认收口", exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: "确认执行已登记创建一次", exact: true }).count(), 0);
    await page.getByLabel("Run ID", { exact: true }).fill(prepared.run_id); await phase(page, prepared.run_id, "completed", tokens.alice_fresh);
    assert.equal(await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), true);
    const beforeFollow = await (await page.request.get(`${profile.runtime}${path}`, { headers: { Authorization: `Bearer ${tokens.alice_fresh}` } })).json();
    const writesBeforeFollow = writes.length;
    await page.getByRole("button", { name: "跟随／续读已持久化事件", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "不推断 Run 完成" }).waitFor();
    assert.equal(writes.length, writesBeforeFollow);
    assert.equal(await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), true);
    assert.deepEqual(await (await page.request.get(`${profile.runtime}${path}`, { headers: { Authorization: `Bearer ${tokens.alice_fresh}` } })).json(), beforeFollow);
    assert.equal(beforeFollow.acknowledged, false);
    const denied = await page.request.post(`${profile.runtime}${path}/acknowledge`, { headers: { Authorization: `Bearer ${tokens.bob}` },
      data: { fingerprint: prepared.fingerprint, revision: committed.revision } });
    assert.equal(denied.status(), 404);
    await page.getByRole("button", { name: "预览本人确认收口", exact: true }).click();
    await page.getByRole("button", { name: "取消收口", exact: true }).click();
    await page.getByRole("button", { name: "预览本人确认收口", exact: true }).click();
    await page.getByRole("button", { name: "确认收口此回执", exact: true }).click();
    await page.waitForFunction(() => ![...document.querySelectorAll("button")].find(button => button.textContent === "登记新 Run（尚未创建）")?.disabled);
    const response = await page.request.get(`${profile.runtime}/v1/runs/${prepared.run_id}/events`, { headers: { Authorization: `Bearer ${tokens.alice_fresh}` } });
    assert.equal(response.status(), 200); const events = await response.json();
    const created = events.filter(event => event.type === "run.created");
    assert.equal(created.length, 1); assert.equal(created[0].seq, 1);
    assert.equal(created[0].payload.workbench_attempt_id, prepared.attempt_id);
    assert.equal(created[0].payload.workbench_value_hmac, prepared.value_hmac);
    assert.equal(writes.filter(value => value === "/v1/runs").length, 0);
    assert.equal(writes.filter(value => value === "/v1/workbench/create-attempts").length, 1);
    assert.equal(writes.filter(value => value === `${path}/execute`).length, 2);
    assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
    await page.getByLabel("Agent ID", { exact: true }).fill("s12-workbench");
    await page.getByRole("button", { name: "读取本人会话", exact: true }).click();
    await page.getByLabel("会话", { exact: true }).selectOption(conversationId);
    await page.getByRole("button", { name: "读取权威历史", exact: true }).click();
    await page.getByRole("checkbox", { name: new RegExp(prepared.run_id) }).check();
    await page.getByLabel("消息", { exact: true }).fill("F2 explicit second question");
    await page.unroute("**/v1/workbench/create-attempts");
    const nextPrepared = page.waitForResponse(response => response.url().endsWith("/v1/workbench/create-attempts") && response.request().method() === "POST");
    await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).click();
    const next = await (await nextPrepared).json();
    assert.notEqual(next.run_id, prepared.run_id);
    await page.getByRole("button", { name: "确认执行已登记创建一次", exact: true }).click();
    await page.getByRole("button", { name: "预览本人确认收口", exact: true }).click();
    await page.getByRole("button", { name: "确认收口此回执", exact: true }).click();
    await phase(page, next.run_id, "completed", tokens.alice_fresh);
    const ownHistory = await page.request.get(`${profile.runtime}/v1/workbench/conversations/${conversationId}`, { headers: { Authorization: `Bearer ${tokens.alice_fresh}` } });
    assert.deepEqual((await ownHistory.json()).items.map(row => row.run_id), [prepared.run_id, next.run_id]);
    const foreignHistory = await page.request.get(`${profile.runtime}/v1/workbench/conversations/${conversationId}`, { headers: { Authorization: `Bearer ${tokens.foreign}` } });
    assert.equal(foreignHistory.status(), 404);
    await second.close();
  } finally { await context.close(); await engine.close(); }
});


test("F2合成附件：失败关闭、UTF8与数量校验、旧扫描响应不能跨身份恢复", async () => {
  const tokens = JSON.parse(await readFile(profile.tokens, "utf8"));
  const engine = await browser(), context = await engine.newContext(), page = await context.newPage();
  page.setDefaultTimeout(5000);
  let writes = 0;
  context.on("request", request => { if (request.method() === "POST" && new URL(request.url()).pathname.includes("create-attempts")) writes++; });
  try {
    await page.goto(`${profile.url}/tests/browser/workbench.html`); await apply(page, tokens.alice);
    await page.getByRole("button", { name: "只读核对写账本", exact: true }).click();
    await page.getByRole("button", { name: "开始新会话（尚未写入）", exact: true }).click();
    const input = page.getByLabel("选择合成txt附件", { exact: true });
    for (const text of ["SYNTHETIC_SCAN_REJECT", "SYNTHETIC_SCAN_UNAVAILABLE"]) {
      await input.setInputFiles({ name: "failure.txt", mimeType: "text/plain", buffer: Buffer.from(text) });
      await page.getByRole("button", { name: "扫描合成附件", exact: true }).click();
      await page.getByLabel("附件状态", { exact: true }).filter({ hasText: "扫描失败" }).waitFor();
      assert.equal(await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), true);
    }
    await input.setInputFiles({ name: "invalid.txt", mimeType: "text/plain", buffer: Buffer.from([0xff, 0xfe]) });
    await page.getByLabel("附件状态", { exact: true }).filter({ hasText: "有效UTF-8" }).waitFor();
    assert.equal(await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), true);
    await input.setInputFiles(Array.from({ length: 4 }, (_, i) => ({ name: `${i}.txt`, mimeType: "text/plain", buffer: Buffer.from("synthetic") })));
    await page.getByLabel("附件状态", { exact: true }).filter({ hasText: "最多3个" }).waitFor();
    assert.equal(await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), true);
    let release; const held = new Promise(resolve => { release = resolve; });
    let seen; const requested = new Promise(resolve => { seen = resolve; });
    await page.route("**/v1/workbench/attachment-preview", async route => { const response = await route.fetch(); seen(); await held; await route.fulfill({ response }); });
    await input.setInputFiles({ name: "safe.txt", mimeType: "text/plain", buffer: Buffer.from("SYNTHETIC-OLD-IDENTITY") });
    await page.getByRole("button", { name: "扫描合成附件", exact: true }).click(); await requested;
    await apply(page, tokens.bob); release();
    assert.equal(await page.getByText("SYNTHETIC-OLD-IDENTITY", { exact: true }).count(), 0);
    assert.equal(await page.getByLabel("合成txt附件", { exact: true }).count(), 0);
    assert.equal(writes, 0);
    assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
  } finally { await context.close(); await engine.close(); }
});


test("F2分页：真实隔离列表翻页、末页、刷新清选与身份隔离，无新写", async () => {
  const tokens = JSON.parse(await readFile(profile.tokens, "utf8"));
  const engine = await browser(), context = await engine.newContext(), page = await context.newPage();
  page.setDefaultTimeout(5000);
  const writes = [];
  context.on("request", request => { if (request.method() === "POST" && new URL(request.url()).pathname.startsWith("/v1/")) writes.push(request.url()); });
  try {
    await page.goto(`${profile.url}/tests/browser/workbench.html`); await apply(page, tokens.pagination);
    await page.getByLabel("Agent ID", { exact: true }).fill("s12-workbench");
    await page.getByRole("button", { name: "只读核对写账本", exact: true }).click();
    await page.getByRole("button", { name: "读取本人会话", exact: true }).click();
    await page.getByLabel("会话分页状态", { exact: true }).filter({ hasText: "本页 20 条" }).waitFor();
    const first = await page.getByLabel("会话", { exact: true }).locator("option").nth(1).getAttribute("value");
    await page.getByRole("button", { name: "会话下一页", exact: true }).click();
    await page.getByLabel("会话分页状态", { exact: true }).filter({ hasText: "本页 5 条" }).waitFor();
    assert.equal(await page.getByRole("button", { name: "会话下一页", exact: true }).isDisabled(), true);
    await page.getByRole("button", { name: "读取本人会话", exact: true }).click();
    await page.getByLabel("会话", { exact: true }).selectOption(first);
    await page.getByRole("button", { name: "读取权威历史", exact: true }).click();
    await page.getByLabel("历史分页状态", { exact: true }).filter({ hasText: "本页 20 条" }).waitFor();
    await page.getByRole("checkbox", { name: /run_pagination_0000/ }).check();
    await page.getByRole("button", { name: "历史下一页", exact: true }).click();
    await page.getByLabel("历史分页状态", { exact: true }).filter({ hasText: "本页 5 条" }).waitFor();
    assert.equal(await page.getByRole("checkbox", { checked: true }).count(), 0);
    assert.equal(await page.getByRole("checkbox", { name: /run_pagination_0000/ }).count(), 0);
    assert.equal(await page.getByRole("button", { name: "历史下一页", exact: true }).isDisabled(), true);
    await page.getByRole("button", { name: "读取权威历史", exact: true }).click();
    await page.getByLabel("历史分页状态", { exact: true }).filter({ hasText: "第 1 页" }).waitFor();
    await page.route("**/v1/workbench/conversations/*?*", route => route.fulfill({ status: 503, json: { detail: "synthetic unavailable" } }));
    await page.getByRole("button", { name: "历史下一页", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "503" }).waitFor();
    assert.equal(await page.getByRole("checkbox").count(), 0);
    assert.equal(await page.getByRole("button", { name: "历史下一页", exact: true }).isDisabled(), true);
    await apply(page, tokens.bob);
    assert.equal(await page.getByLabel("会话分页状态", { exact: true }).count(), 0);
    assert.equal(await page.getByLabel("历史分页状态", { exact: true }).count(), 0);
    assert.equal(writes.length, 0);
  } finally { await context.close(); await engine.close(); }
});


test("F2 Portal收口：第二页历史＋附件登记、重载恢复、本人ACK与同Run轨迹", async () => {
  const tokens = JSON.parse(await readFile(profile.tokens, "utf8"));
  const engine = await browser(), context = await engine.newContext(), page = await context.newPage();
  page.setDefaultTimeout(5000);
  const registrations = [], executions = [];
  page.on("request", request => {
    if (request.method() !== "POST") return;
    const path = new URL(request.url()).pathname;
    if (path === "/v1/workbench/create-attempts") registrations.push(request.postDataJSON());
    if (path.endsWith("/execute")) executions.push(path);
  });
  await page.route("**/synthetic-tokens", route => route.fulfill({ json: tokens }));
  async function openPortal() {
    await page.getByLabel("合成身份", { exact: true }).selectOption("pagination");
    await page.getByRole("link", { name: "s12-workbench", exact: true }).click();
    await page.getByRole("button", { name: "应用门户登录身份并核对账本", exact: true }).click();
    await page.getByRole("status").filter({ hasText: /验签身份与账本读取完成|已恢复服务端活动账本/ }).waitFor();
  }
  try {
    await page.goto(`${profile.url}/tests/browser/portal-flow.html`); await openPortal();
    assert.equal(await page.getByLabel("Agent ID", { exact: true }).getAttribute("readonly"), "");
    await page.getByRole("button", { name: "读取本人会话", exact: true }).click();
    await page.getByLabel("会话", { exact: true }).selectOption("00000000-0000-0000-0000-0000000003e8");
    await page.getByRole("button", { name: "读取权威历史", exact: true }).click();
    await page.getByRole("button", { name: "历史下一页", exact: true }).click();
    await page.getByRole("checkbox", { name: /run_pagination_0024/ }).check();
    await page.getByLabel("选择合成txt附件", { exact: true }).setInputFiles({ name: "portal-synthetic.txt", mimeType: "text/plain", buffer: Buffer.from("PORTAL-SYNTHETIC-ONLY") });
    await page.getByRole("button", { name: "扫描合成附件", exact: true }).click();
    await page.getByLabel("附件状态", { exact: true }).filter({ hasText: "合成扫描通过" }).waitFor();
    const question = "F2 Portal synthetic continuation";
    await page.getByLabel("消息", { exact: true }).fill(question);
    const registered = page.waitForResponse(response => new URL(response.url()).pathname === "/v1/workbench/create-attempts" && response.request().method() === "POST");
    await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).click();
    const response = await registered; assert.equal(response.status(), 200); const row = await response.json();
    assert.deepEqual(registrations[0].selected_run_ids, ["run_pagination_0024"]);
    assert.equal(registrations[0].synthetic_attachments[0].text, "PORTAL-SYNTHETIC-ONLY");
    await page.reload(); await openPortal();
    assert.equal(await page.getByLabel("登记时的原始创建消息", { exact: true }).inputValue(), "");
    await page.getByRole("button", { name: "只读核对登记附件摘要", exact: true }).click();
    await page.getByText(/portal-synthetic.txt · .*合成扫描通过/).waitFor();
    assert.equal((await page.locator("body").innerText()).includes("PORTAL-SYNTHETIC-ONLY"), false);
    assert.equal(executions.length, 0);
    await page.getByLabel("登记时的原始创建消息", { exact: true }).fill(question);
    const executed = page.waitForResponse(response => new URL(response.url()).pathname === `/v1/workbench/attempts/${row.attempt_id}/execute` && response.request().method() === "POST");
    await page.getByRole("button", { name: "确认执行已登记创建一次", exact: true }).click();
    const executionResponse = await executed; assert.equal(executionResponse.status(), 200);
    assert.equal((await executionResponse.json()).status, "COMMITTED");
    await phase(page, row.run_id, "completed", tokens.pagination);
    assert.equal(await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), true);
    await page.getByRole("button", { name: "预览本人确认收口", exact: true }).click();
    await page.getByRole("button", { name: "确认收口此回执", exact: true }).click();
    await page.getByLabel("Run ID", { exact: true }).fill(row.run_id);
    await page.getByRole("button", { name: "跟随／续读已持久化事件", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "不推断 Run 完成" }).waitFor();
    assert.equal(registrations.length, 1); assert.equal(executions.length, 1);
    const events = await (await page.request.get(`${profile.runtime}/v1/runs/${row.run_id}/events`, { headers: { Authorization: `Bearer ${tokens.pagination}` } })).json();
    assert.equal(events.filter(event => event.type === "run.created").length, 1);
    assert.equal(events.every(event => event.run_id === row.run_id), true);
    assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
  } finally { await context.close(); await engine.close(); }
});
