import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { apply, browser, profile, ledgerIdentity } from "./support.mjs";
const row = { scope_id: ledgerIdentity.scope_id, attempt_id: "11111111-1111-4111-8111-111111111111", run_id: "run1", action: "feedback",
  request_seq: 4, fingerprint: "b".repeat(64), payload_hash: "b".repeat(64), score: 1, score_revision: 1,
  status: "PENDING_OBSERVATION", revision: 2, evidence_seq: null, rejection: null, acknowledged: false };
test("反馈DOM：未决只允许对账、坏来源拒收、OBSERVED不自动解锁", async () => {
  const engine = await browser(), context = await engine.newContext(), page = await context.newPage();
  page.setDefaultTimeout(5000); let current = row; const calls = [];
  await context.route("**/v1/**", route => {
    const path = new URL(route.request().url()).pathname; calls.push(route.request().method());
    return route.fulfill({ json: path.endsWith("identity") ? ledgerIdentity : path.endsWith("attempts") ? { scope_id: ledgerIdentity.scope_id, active: current } : current });
  });
  try {
    await page.goto(`${profile.url}/tests/browser/workbench.html`); await apply(page, "synthetic");
    await page.getByRole("button", { name: "只读对账反馈来源（不重发）", exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: "预览本人确认收口", exact: true }).count(), 0);
    assert.equal(await page.getByRole("button", { name: "确认发送已登记反馈一次", exact: true }).count(), 0);
    current = { ...row, payload_hash: "c".repeat(64) };
    await page.getByRole("button", { name: "只读核对写账本", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "反馈回执绑定无效" }).waitFor();
    assert.equal(await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), true);
    current = { ...row, status: "OBSERVED", revision: 3 };
    await page.getByRole("button", { name: "只读核对写账本", exact: true }).click();
    await page.getByRole("button", { name: "预览本人确认收口", exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), true);
    assert.ok(calls.every(method => method === "GET"));
  } finally { await context.close(); await engine.close(); }
});
test("反馈DOM：忽略abort的旧明细不跨身份显示或解锁", async () => {
  const engine = await browser(), context = await engine.newContext(), page = await context.newPage();
  page.setDefaultTimeout(5000);
  await context.addInitScript(({ identity, receipt }) => {
    const native = window.fetch.bind(window);
    window.feedbackHold = { release: null, held: false };
    window.fetch = async (path, options = {}) => {
      const old = options.headers?.Authorization === "Bearer old";
      if (String(path).endsWith("/identity")) return Response.json(old ? identity : { ...identity, scope_id: "d".repeat(64), subject: "human:new" });
      if (String(path) === "/v1/workbench/attempts") return Response.json({ scope_id: old ? identity.scope_id : "d".repeat(64), active: old ? receipt : null });
      if (String(path).includes("/feedback-attempts/")) {
        window.feedbackHold.held = true;
        return new Promise(resolve => { window.feedbackHold.release = () => resolve(Response.json(receipt)); });
      }
      return native(path, options);
    };
  }, { identity: ledgerIdentity, receipt: row });
  try {
    await page.goto(`${profile.url}/tests/browser/workbench.html`);
    await page.getByLabel("用户访问令牌", { exact: true }).fill("old");
    await page.getByRole("button", { name: "应用身份并清空旧数据", exact: true }).click();
    await page.waitForFunction(() => window.feedbackHold.held);
    await apply(page, "new");
    await page.evaluate(() => window.feedbackHold.release());
    await page.getByText(`synthetic / human:new · ${"d".repeat(64)}`, { exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: "只读对账反馈来源（不重发）", exact: true }).count(), 0);
    assert.equal((await page.locator("body").innerText()).includes(row.attempt_id), false);
  } finally { await context.close(); await engine.close(); }
});
test("真实可信反馈：登记丢响应、重载、双标签竞争、发送断连、同主体恢复及本人收口", async () => {
  const tokens = JSON.parse(await readFile(profile.tokens, "utf8")), fixture = JSON.parse(process.env.S12_FEEDBACK_FIXTURE);
  const engine = await browser(), context = await engine.newContext(), page = await context.newPage(), peer = await context.newPage();
  page.setDefaultTimeout(5000); peer.setDefaultTimeout(5000);
  const requests = []; context.on("request", request => { if (request.method() === "POST" && request.url().includes("feedback-attempts")) requests.push(new URL(request.url()).pathname); });
  try {
    await page.goto(`${profile.url}/tests/browser/workbench.html`); await apply(page, tokens.alice);
    await page.getByLabel("Run ID", { exact: true }).fill(fixture.run_id);
    await page.getByRole("button", { name: "读取 Run 与待办卡片", exact: true }).click();
    await page.getByRole("button", { name: "读取反馈当前修订", exact: true }).click();
    await page.getByText("当前修订 0", { exact: true }).waitFor();
    let prepared;
    await page.route("**/v1/workbench/feedback-attempts", async route => {
      const response = await route.fetch(); assert.equal(response.status(), 200); prepared = await response.json(); await route.abort("failed");
    });
    await page.getByRole("button", { name: "登记反馈（尚未发送）", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "结果未知" }).waitFor();
    assert.equal(requests.filter(path => path.endsWith("execute")).length, 0);
    await page.reload(); await apply(page, tokens.alice_fresh);
    await page.getByRole("button", { name: "确认发送已登记反馈一次", exact: true }).waitFor();
    await peer.goto(`${profile.url}/tests/browser/workbench.html`); await apply(peer, tokens.alice);
    await peer.getByRole("button", { name: "确认发送已登记反馈一次", exact: true }).waitFor();
    const path = `/v1/workbench/feedback-attempts/${prepared.attempt_id}`;
    let entered = 0, release; const barrier = new Promise(resolve => { release = resolve; }), statuses = [];
    await context.route(`**${path}/execute`, async route => {
      entered++; if (entered === 2) release(); await barrier;
      const response = await route.fetch(); statuses.push(response.status()); await route.abort("failed");
    });
    await Promise.all([page, peer].map(tab => tab.getByRole("button", { name: "确认发送已登记反馈一次", exact: true }).click()));
    await Promise.all([page, peer].map(tab => tab.getByRole("status").filter({ hasText: "结果未知" }).waitFor()));
    assert.deepEqual(statuses.sort(), [200, 409]);
    await page.reload(); await apply(page, tokens.alice_fresh);
    await page.getByRole("button", { name: "只读对账反馈来源（不重发）", exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: "确认发送已登记反馈一次", exact: true }).count(), 0);
    const pending = await (await page.request.get(`${profile.runtime}${path}`, { headers: { Authorization: `Bearer ${tokens.alice}` } })).json();
    assert.equal(pending.status, "PENDING_OBSERVATION");
    for (const token of [tokens.bob, tokens.foreign]) {
      assert.equal((await page.request.get(`${profile.runtime}${path}`, { headers: { Authorization: `Bearer ${token}` } })).status(), 404);
    }
    await apply(peer, tokens.bob);
    assert.equal(await peer.getByRole("button", { name: "只读对账反馈来源（不重发）", exact: true }).count(), 0);
    let observed;
    await page.route(`**${path}/reconcile`, async route => {
      const response = await route.fetch(); assert.equal(response.status(), 200); observed = await response.json(); await route.abort("failed");
    });
    await page.getByRole("button", { name: "只读对账反馈来源（不重发）", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "结果未知" }).waitFor();
    assert.equal(observed.status, "OBSERVED");
    await page.getByRole("button", { name: "只读核对写账本", exact: true }).click();
    await page.getByRole("button", { name: "预览本人确认收口", exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), true);
    assert.equal((await page.request.post(`${profile.runtime}${path}/acknowledge`, { headers: { Authorization: `Bearer ${tokens.bob}` },
      data: { fingerprint: observed.fingerprint, revision: observed.revision } })).status(), 404);
    await page.getByRole("button", { name: "预览本人确认收口", exact: true }).click();
    await page.getByRole("button", { name: "取消收口", exact: true }).click();
    assert.equal(await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), true);
    await page.getByRole("button", { name: "预览本人确认收口", exact: true }).click();
    await page.route(`**${path}/acknowledge`, async route => {
      const response = await route.fetch(); assert.equal(response.status(), 200); await route.abort("failed");
    });
    await page.getByRole("button", { name: "确认收口此回执", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "结果未知" }).waitFor();
    assert.equal(await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), true);
    await page.getByRole("button", { name: "只读核对写账本", exact: true }).click();
    await page.getByRole("button", { name: "确认已收口反馈（仅只读恢复）", exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), true);
    await page.getByRole("button", { name: "确认已收口反馈（仅只读恢复）", exact: true }).click();
    await page.waitForFunction(() => ![...document.querySelectorAll("button")].find(button => button.textContent === "登记新 Run（尚未创建）")?.disabled);
    assert.equal(requests.filter(value => value === `${path}/execute`).length, 2);
    assert.equal(requests.filter(value => value === "/v1/workbench/feedback-attempts").length, 1);
    assert.equal(requests.filter(value => value === `${path}/acknowledge`).length, 1);
    assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
  } finally { await context.close(); await engine.close(); }
});
