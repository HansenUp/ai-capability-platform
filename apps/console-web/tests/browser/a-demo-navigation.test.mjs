import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { browser, phase, profile } from "./support.mjs";

test("整合演示：身份切换与跨页定位不解除未决写，显式执行及本人ACK闭环保留", async () => {
  const engine = await browser(), context = await engine.newContext(), page = await context.newPage();
  page.setDefaultTimeout(5000);
  const tokens = JSON.parse(await readFile(profile.tokens, "utf8"));
  const app = () => page.getByRole("button", { name: "应用演示身份并定位同Run", exact: true });
  async function select(role) {
    await page.getByLabel("演示测试主体", { exact: true }).selectOption(role);
    await app().click();
    await page.waitForFunction(() => ![...document.querySelectorAll("button")].find(button => button.textContent === "只读核对写账本")?.disabled);
  }
  async function acknowledge() {
    await page.getByRole("button", { name: "预览本人确认收口", exact: true }).click();
    await page.getByRole("button", { name: "确认收口此回执", exact: true }).click();
    await page.waitForFunction(() => ![...document.querySelectorAll("button")].find(button => button.textContent === "登记新 Run（尚未创建）")?.disabled);
  }
  try {
    await page.goto(profile.url + "/chat");
    await page.getByLabel("演示测试主体", { exact: true }).waitFor();
    await page.waitForFunction(() => !document.querySelector('aside[aria-label="独立演示导航"] select')?.disabled);
    await select("alice");
    console.log("DEMO_NAV: Alice explicitly applied");
    await page.getByLabel("消息", { exact: true }).fill("S12_E2E_APPROVAL_THEN_INPUT");
    const registration = page.waitForResponse(response => new URL(response.url()).pathname === "/v1/workbench/create-attempts");
    await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).click();
    const attempt = await (await registration).json(); assert.equal(attempt.status, "PREPARED");
    console.log("DEMO_NAV: create prepared before navigation");
    await page.getByRole("link", { name: "③ 同Run只读轨迹", exact: true }).click();
    await page.getByRole("heading", { name: "Run 只读调试轨迹", exact: true }).waitFor();
    await page.getByRole("link", { name: "② 对话／审批／补充输入", exact: true }).click();
    await app().click();
    await page.getByRole("button", { name: "确认执行已登记创建一次", exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true }).isDisabled(), true);
    await page.getByLabel("登记时的原始创建消息", { exact: true }).fill("S12_E2E_APPROVAL_THEN_INPUT");
    await page.getByRole("button", { name: "确认执行已登记创建一次", exact: true }).click();
    await acknowledge();
    const runId = await page.getByLabel("Run ID", { exact: true }).inputValue();
    assert.equal(runId, attempt.run_id);
    await phase(page, runId, "auth-required", tokens.alice);
    await page.getByLabel("演示Run定位", { exact: true }).fill(runId);
    await select("bob");
    assert.equal(await page.getByRole("heading", { name: new RegExp(runId) }).count(), 0);
    await page.getByRole("button", { name: "读取 Run 与待办卡片", exact: true }).click();
    await page.getByRole("button", { name: "预览批准", exact: true }).waitFor();
    await page.getByRole("button", { name: "预览批准", exact: true }).click();
    await page.getByRole("button", { name: "确认登记一次", exact: true }).click();
    await page.getByRole("button", { name: "确认执行已登记批准一次", exact: true }).click();
    await phase(page, runId, "input-required", tokens.bob);
    await acknowledge();
    await select("alice");
    await page.getByRole("button", { name: "读取 Run 与待办卡片", exact: true }).click();
    await page.locator("section.card textarea").fill('{"id":"SYNTHETIC-E2E"}');
    await page.getByRole("button", { name: "登记回答一次", exact: true }).click();
    await page.getByRole("button", { name: "确认执行已登记回答一次", exact: true }).click();
    await acknowledge();
    await phase(page, runId, "completed", tokens.alice);
    await page.getByRole("link", { name: "③ 同Run只读轨迹", exact: true }).click();
    await app().click();
    assert.equal(await page.getByLabel("Run ID", { exact: true }).inputValue(), runId);
    await page.getByRole("button", { name: "读取指定 Run", exact: true }).click();
    await page.getByRole("heading", { name: `${runId} · completed`, exact: true }).waitFor();
    await page.getByLabel("演示测试主体", { exact: true }).selectOption("bob");
    assert.equal(await page.getByRole("heading", { name: `${runId} · completed`, exact: true }).count(), 0);
    assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
    await page.reload(); await page.getByLabel("演示测试主体", { exact: true }).waitFor();
    assert.equal(await page.getByLabel("演示测试主体", { exact: true }).inputValue(), "");
    assert.equal(await page.getByLabel("演示Run定位", { exact: true }).inputValue(), "");
  } finally { await context.close(); await engine.close(); }
});
