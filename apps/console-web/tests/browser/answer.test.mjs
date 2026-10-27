import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { apply, browser, phase, profile } from "./support.mjs";

test("真实回答账本：重载不恢复原值，换值被拒，提交丢响应后仅原操作者收口", async () => {
  const tokens = JSON.parse(await readFile(profile.tokens, "utf8"));
  const engine = await browser(), context = await engine.newContext(), page = await context.newPage();
  page.setDefaultTimeout(5000);
  const writes = [];
  page.on("request", request => {
    if (request.method() === "POST" && new URL(request.url()).pathname.startsWith("/v1/workbench/")) writes.push(new URL(request.url()).pathname);
  });
  try {
    const created = await page.request.post(`${profile.runtime}/v1/runs`, { headers: { Authorization: `Bearer ${tokens.alice}` },
      data: { agent_id: "s12-workbench", input: [{ role: "user", content: "S12_E2E_APPROVAL_THEN_INPUT" }] } });
    assert.equal(created.status(), 202); const runId = (await created.json()).run_id;
    await page.goto(`${profile.url}/tests/browser/workbench.html`); await apply(page, tokens.bob);
    await page.getByLabel("Run ID", { exact: true }).fill(runId); await phase(page, runId, "auth-required", tokens.bob);
    await page.getByRole("button", { name: "预览批准", exact: true }).click();
    await page.getByRole("button", { name: "确认登记一次", exact: true }).click();
    await page.getByRole("button", { name: "确认执行已登记批准一次", exact: true }).click();
    await page.getByRole("button", { name: "预览本人确认收口", exact: true }).click();
    await page.getByRole("button", { name: "确认收口此回执", exact: true }).click();
    await page.waitForFunction(() => ![...document.querySelectorAll("button")].find(button => button.textContent === "登记新 Run（尚未创建）")?.disabled);
    await apply(page, tokens.alice); await page.getByLabel("Run ID", { exact: true }).fill(runId);
    await phase(page, runId, "input-required", tokens.alice);
    const value = { id: "ANSWER-RELOAD-SYNTHETIC" };
    await page.locator("section.card textarea").fill(JSON.stringify(value));
    const registered = page.waitForResponse(response => new URL(response.url()).pathname === "/v1/workbench/attempts" && response.request().method() === "POST");
    await page.getByRole("button", { name: "登记回答一次", exact: true }).click();
    const registration = await registered; assert.equal(registration.status(), 200);
    const prepared = await registration.json(), path = `/v1/workbench/attempts/${prepared.attempt_id}`;
    assert.equal(JSON.stringify(prepared).includes(value.id), false);
    await page.reload(); await apply(page, tokens.alice_fresh);
    assert.equal(await page.getByLabel("登记时的原始 JSON 回答", { exact: true }).inputValue(), "");
    assert.equal((await page.locator("body").innerText()).includes(value.id), false);
    await page.getByLabel("登记时的原始 JSON 回答", { exact: true }).fill('{"id":"DIFFERENT"}');
    await page.getByRole("button", { name: "确认执行已登记回答一次", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "Runtime HTTP 409" }).waitFor();
    await page.getByRole("button", { name: "只读核对写账本", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "已恢复服务端活动账本" }).waitFor();
    let committed;
    await page.route(`**${path}/execute`, async route => {
      const response = await route.fetch(); assert.equal(response.status(), 200); committed = await response.json();
      await route.abort("failed");
    });
    await page.getByLabel("登记时的原始 JSON 回答", { exact: true }).fill(JSON.stringify(value));
    await page.getByRole("button", { name: "确认执行已登记回答一次", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "结果未知" }).waitFor();
    assert.equal(committed.status, "COMMITTED");
    await page.reload(); await apply(page, tokens.alice_fresh);
    await page.getByRole("button", { name: "预览本人确认收口", exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: "确认执行已登记回答一次", exact: true }).count(), 0);
    const foreign = await page.request.post(`${profile.runtime}${path}/acknowledge`, { headers: { Authorization: `Bearer ${tokens.bob}` },
      data: { fingerprint: prepared.fingerprint, revision: committed.revision } });
    assert.equal(foreign.status(), 404);
    await page.getByLabel("Run ID", { exact: true }).fill(runId); await phase(page, runId, "completed", tokens.alice_fresh);
    assert.equal(await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), true);
    await page.getByRole("button", { name: "预览本人确认收口", exact: true }).click();
    await page.getByRole("button", { name: "确认收口此回执", exact: true }).click();
    await page.waitForFunction(() => ![...document.querySelectorAll("button")].find(button => button.textContent === "登记新 Run（尚未创建）")?.disabled);
    const response = await page.request.get(`${profile.runtime}/v1/runs/${runId}/events`, { headers: { Authorization: `Bearer ${tokens.alice_fresh}` } });
    assert.equal(response.status(), 200); const events = await response.json();
    const receipts = events.filter(event => event.payload.workbench_attempt_id === prepared.attempt_id);
    assert.equal(receipts.length, 1); assert.equal(receipts[0].type, "input.provided");
    assert.equal(receipts[0].payload.workbench_value_hmac, prepared.value_hmac);
    assert.equal(writes.filter(value => value === `${path}/execute`).length, 2);
    assert.equal(writes.filter(value => value === `${path}/acknowledge`).length, 1);
    assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
  } finally { await context.close(); await engine.close(); }
});
