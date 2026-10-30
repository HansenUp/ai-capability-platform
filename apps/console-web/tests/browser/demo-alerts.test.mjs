import { test } from "node:test";
import assert from "node:assert/strict";
import { browser, profile } from "./support.mjs";
test("告警演示入口：无需复制令牌、本人操作、身份切换及到期清空", async () => {
  const engine = await browser(), context = await engine.newContext(), page = await context.newPage();
  page.setDefaultTimeout(5000);
  const writes = [];
  page.on("request", request => { if (request.method() === "POST" && request.url().includes("isolated-alerts")) writes.push(request); });
  try {
    await page.goto(profile.url + "/demo");
    await page.getByRole("region", { name: "告警体验指引" }).waitFor();
    const checklist = page.getByRole("region", { name: "F3统一体验清单", exact: true });
    await checklist.waitFor();
    for (const [name, href] of [["单任务用量与费用", "/chat"], ["Trace用量与费用", "/manage/operations"], ["评测与发布指引", "/manage/agents"], ["可信反馈", "/manage/operations#feedback-trusted"], ["历史评分审计", "/manage/operations#feedback-history"], ["合成告警与通知历史", "/manage/alerts"]])
      assert.equal(await checklist.getByRole("link", { name, exact: true }).getAttribute("href"), href);
    assert.match(await checklist.innerText(), /普通演示不自动注入此身份或可信样本/);
    assert.match(await checklist.innerText(), /北极星仍待定义/);
    await page.goto(profile.url + "/manage/alerts");
    const apply = page.getByRole("button", { name: "应用告警演示身份", exact: true });
    await page.getByRole("link", { name: "④ 合成告警体验", exact: true }).waitFor();
    await apply.click();
    const read = page.getByRole("button", { name: "读取告警列表", exact: true });
    async function detail() { await read.click(); await page.getByRole("button", { name: "查看告警 synthetic-alert", exact: true }).click(); }
    await detail();
    for (const name of ["预览本人认领", "预览显式处理", "预览模拟通知"]) {
      await page.getByRole("button", { name, exact: true }).click();
      await page.getByRole("button", { name: "确认记录一次", exact: true }).click();
      await page.getByRole("status").filter({ hasText: "操作已记录" }).waitFor();
    }
    await page.getByRole("listitem").filter({ hasText: "simulated_received · in-memory-synthetic" }).waitFor();
    await page.getByText("已处理，但信号尚未证实恢复。", { exact: true }).waitFor();
    await page.getByLabel("告警演示管理员", { exact: true }).selectOption("bob");
    assert.equal(writes.length, 3);
    await apply.click();
    assert.equal(await page.getByRole("region", { name: "告警详情", exact: true }).count(), 0);
    await detail();
    assert.equal(await page.getByRole("button", { name: "预览模拟通知", exact: true }).count(), 0);
    assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
    assert.equal(await page.getByLabel("告警测试管理员令牌", { exact: true }).inputValue(), "");
    await page.route("**/__demo/info", route => route.fulfill({ json: { expires_at: "2000-01-01T00:00:00Z", tokens: {} } }));
    await page.getByRole("button", { name: "重读隔离演示信息", exact: true }).click();
    await page.getByText(/隔离演示信息不可用/).waitFor();
    assert.equal(await apply.isDisabled(), true);
    assert.equal(await page.getByRole("region", { name: "告警详情", exact: true }).count(), 0);
    assert.equal(writes.length, 3);
  } finally { await context.close(); await engine.close(); }
});
