import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { apply, browser, phase, profile } from "./support.mjs";

for (const action of ["approve", "reject"]) test(`真实审批账本 ${action}：Bob登记Alice的Run，重载／丢响应保持Bob锁，精确回执后本人收口`, async () => {
  const tokens = JSON.parse(await readFile(profile.tokens, "utf8"));
  const engine = await browser(), context = await engine.newContext(), page = await context.newPage();
  page.setDefaultTimeout(5000);
  const writes = [];
  page.on("request", request => {
    const path = new URL(request.url()).pathname;
    if (request.method() === "POST" && path.startsWith("/v1/workbench/")) writes.push({ path, body: request.postDataJSON(), token: request.headers().authorization });
  });
  try {
    const created = await page.request.post(`${profile.runtime}/v1/runs`, { headers: { Authorization: `Bearer ${tokens.alice}` },
      data: { agent_id: "s12-workbench", input: [{ role: "user", content: "S12_E2E_APPROVAL_THEN_INPUT" }] } });
    assert.equal(created.status(), 202); const runId = (await created.json()).run_id;
    await page.goto(`${profile.url}/tests/browser/workbench.html`); await apply(page, tokens.bob);
    await page.getByLabel("Run ID", { exact: true }).fill(runId);
    await phase(page, runId, "auth-required", tokens.bob);
    await page.getByRole("button", { name: action === "approve" ? "预览批准" : "预览拒绝", exact: true }).click();
    const registered = page.waitForResponse(response => new URL(response.url()).pathname === "/v1/workbench/attempts" && response.request().method() === "POST");
    await page.getByRole("button", { name: "确认登记一次", exact: true }).click();
    const response = await registered; assert.equal(response.status(), 200);
    const prepared = await response.json(), path = `/v1/workbench/attempts/${prepared.attempt_id}`;
    assert.equal(prepared.action, action);
    await page.reload(); await apply(page, tokens.bob);
    const executeName = action === "approve" ? "确认执行已登记批准一次" : "确认执行已登记拒绝一次";
    await page.getByRole("button", { name: executeName, exact: true }).waitFor();
    assert.equal(writes.length, 1);
    const ownerPage = await context.newPage();
    await ownerPage.goto(`${profile.url}/tests/browser/workbench.html`); await apply(ownerPage, tokens.alice);
    assert.equal(await ownerPage.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), false);
    assert.equal(await ownerPage.getByRole("region", { name: "活动写回执", exact: true }).count(), 0);
    const denied = await ownerPage.request.get(`${profile.runtime}${path}`, { headers: { Authorization: `Bearer ${tokens.alice}` } });
    assert.equal(denied.status(), 404); await ownerPage.close();
    let committed;
    await page.route(`**${path}/execute`, async route => {
      const accepted = await route.fetch(); assert.equal(accepted.status(), 200); committed = await accepted.json();
      assert.equal(committed.status, "COMMITTED"); await route.abort("failed");
    });
    await page.getByRole("button", { name: executeName, exact: true }).click();
    await page.getByRole("status").filter({ hasText: "结果未知" }).waitFor();
    assert.equal(committed.attempt_id, prepared.attempt_id);
    await page.reload(); await apply(page, tokens.bob);
    await page.getByRole("button", { name: "预览本人确认收口", exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: executeName, exact: true }).count(), 0);
    await page.getByLabel("Run ID", { exact: true }).fill(runId);
    await phase(page, runId, action === "approve" ? "input-required" : "failed", tokens.bob);
    assert.equal(await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), true);
    const blocked = await page.request.post(`${profile.runtime}/v1/runs/${runId}/${action}`, { headers: { Authorization: `Bearer ${tokens.bob}` },
      data: { call_id: prepared.call_id, params_hash: prepared.params_hash } });
    assert.equal(blocked.status(), 409);
    const forbiddenAck = await page.request.post(`${profile.runtime}${path}/acknowledge`, { headers: { Authorization: `Bearer ${tokens.alice}` },
      data: { fingerprint: prepared.fingerprint, revision: committed.revision } });
    assert.equal(forbiddenAck.status(), 404);
    await page.getByRole("button", { name: "预览本人确认收口", exact: true }).click();
    await page.getByRole("button", { name: "取消收口", exact: true }).click();
    assert.equal(writes.length, 2);
    await page.getByRole("button", { name: "预览本人确认收口", exact: true }).click();
    await page.getByRole("button", { name: "确认收口此回执", exact: true }).click();
    await page.waitForFunction(() => ![...document.querySelectorAll("button")].find(button => button.textContent === "登记新 Run（尚未创建）")?.disabled);
    const eventResponse = await page.request.get(`${profile.runtime}/v1/runs/${runId}/events`, { headers: { Authorization: `Bearer ${tokens.alice}` } });
    assert.equal(eventResponse.status(), 200); const events = await eventResponse.json();
    const receipts = events.filter(event => event.payload.workbench_attempt_id === prepared.attempt_id);
    assert.equal(receipts.length, 1);
    assert.equal(receipts[0].type, action === "approve" ? "tool.approved" : "tool.rejected");
    assert.equal(receipts[0].payload.approver, "human:bob");
    assert.equal(receipts[0].payload.params_hash, prepared.params_hash);
    assert.equal(events.filter(event => event.type === "tool.executed").length, action === "approve" ? 1 : 0);
    if (action === "approve") assert.equal(events.find(event => event.type === "tool.result" && event.payload.output?.external_effect === false) !== undefined, true);
    assert.deepEqual(writes.map(row => row.path), ["/v1/workbench/attempts", `${path}/execute`, `${path}/acknowledge`]);
    assert.equal(writes.every(row => row.token === `Bearer ${tokens.bob}`), true);
    for (const token of Object.values(tokens)) assert.equal(JSON.stringify(events).includes(token), false);
    assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
  } finally { await context.close(); await engine.close(); }
});
