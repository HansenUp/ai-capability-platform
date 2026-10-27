import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { apply, browser, phase, profile } from "./support.mjs";

test("真实创建账本：无RunID丢登记响应、重载恢复、双标签执行只创建一次、本人收口", async () => {
  const tokens = JSON.parse(await readFile(profile.tokens, "utf8"));
  const engine = await browser(), context = await engine.newContext(), page = await context.newPage();
  page.setDefaultTimeout(5000);
  const writes = [];
  context.on("request", request => {
    const path = new URL(request.url()).pathname;
    if (request.method() === "POST" && path.startsWith("/v1/")) writes.push(path);
  });
  try {
    await page.goto(`${profile.url}/tests/browser/workbench.html`); await apply(page, tokens.alice);
    const original = "CREATE-DURABLE-SYNTHETIC";
    let prepared;
    await page.route("**/v1/workbench/create-attempts", async route => {
      const response = await route.fetch(); assert.equal(response.status(), 200); prepared = await response.json();
      assert.equal(prepared.status, "PREPARED"); await route.abort("failed");
    });
    await page.getByLabel("Agent ID", { exact: true }).fill("s12-workbench");
    await page.getByLabel("消息", { exact: true }).fill(original);
    await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "结果未知" }).waitFor();
    assert.equal(await page.getByLabel("Run ID", { exact: true }).inputValue(), "");
    assert.equal(await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), true);
    assert.equal(writes.length, 1);
    const absent = await page.request.get(`${profile.runtime}/v1/runs/${prepared.run_id}`, { headers: { Authorization: `Bearer ${tokens.alice}` } });
    assert.equal(absent.status(), 404);
    await page.reload(); await apply(page, tokens.alice_fresh);
    await page.getByRole("button", { name: "确认执行已登记创建一次", exact: true }).waitFor();
    assert.equal(await page.getByLabel("登记时的原始创建消息", { exact: true }).inputValue(), "");
    assert.equal((await page.locator("body").innerText()).includes(original), false);
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
    await second.close();
  } finally { await context.close(); await engine.close(); }
});
