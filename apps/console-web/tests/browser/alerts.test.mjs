import { before, after, test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { browser, profile } from "./support.mjs";
let engine, tokens;
before(async () => { engine = await browser(); tokens = JSON.parse(await readFile(profile.tokens, "utf8")); });
after(async () => { await engine?.close(); });
async function identity(page, name) {
  await page.getByLabel("告警测试管理员令牌", { exact: true }).fill(tokens[name]);
  await page.getByRole("button", { name: "应用告警身份并清空", exact: true }).click();
}
async function load(page) {
  await page.getByRole("button", { name: "读取告警列表", exact: true }).click();
  await page.getByRole("button", { name: "查看告警 synthetic-alert", exact: true }).click();
  await page.getByRole("region", { name: "告警详情", exact: true }).waitFor();
}
test("F3告警：真实隔离验签、双标签CAS、本人处理、丢响应只读恢复及模拟通知历史", async () => {
  const context = await engine.newContext(), page = await context.newPage(), second = await context.newPage();
  page.setDefaultTimeout(5000); second.setDefaultTimeout(5000);
  const writes = [];
  context.on("request", req => { if (req.method() === "POST" && req.url().includes("/isolated-alerts/")) writes.push(req); });
  try {
    for (const tab of [page, second]) { await tab.goto(profile.url + "/manage/alerts"); await identity(tab, "alert_alice"); await load(tab); }
    await page.getByRole("button", { name: "预览本人认领", exact: true }).click();
    await page.getByRole("button", { name: "取消告警操作", exact: true }).click();
    assert.equal(writes.length, 0);
    await page.getByRole("button", { name: "预览本人认领", exact: true }).click();
    await second.getByRole("button", { name: "预览本人认领", exact: true }).click();
    await page.getByRole("button", { name: "确认记录一次", exact: true }).click();
    await page.getByText(/信号：abnormal · 处理：claimed/).waitFor();
    await second.getByRole("button", { name: "确认记录一次", exact: true }).click();
    await second.getByRole("status").filter({ hasText: "409" }).waitFor();
    await page.route("**/isolated-alerts/synthetic-alert/actions", async route => { await route.fetch(); await route.abort("failed"); }, { times: 1 });
    await page.getByRole("button", { name: "预览显式处理", exact: true }).click();
    await page.getByRole("button", { name: "确认记录一次", exact: true }).click();
    await page.getByRole("alert").filter({ hasText: "操作结果未知" }).waitFor();
    const count = writes.length;
    await page.getByRole("button", { name: "只读核对原操作回执", exact: true }).click();
    await page.getByText("已处理，但信号尚未证实恢复。", { exact: true }).waitFor();
    assert.equal(writes.length, count);
    await page.getByRole("button", { name: "预览模拟通知", exact: true }).click();
    await page.getByText(/通知预览：合成节点健康异常/).waitFor();
    await page.getByRole("button", { name: "确认记录一次", exact: true }).click();
    await page.getByRole("listitem").filter({ hasText: "simulated_received · in-memory-synthetic" }).waitFor();
    await page.reload(); await identity(page, "alert_alice"); await load(page);
    await page.getByText("已处理，但信号尚未证实恢复。", { exact: true }).waitFor();
    await identity(page, "alert_bob"); await load(page);
    assert.equal(await page.getByRole("button", { name: "预览模拟通知", exact: true }).count(), 0);
    await identity(page, "owner_alice");
    await page.getByRole("button", { name: "读取告警列表", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "403" }).waitFor();
    assert.equal(await page.getByRole("region", { name: "告警详情", exact: true }).count(), 0);
    assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
  } finally { await context.close(); }
});
test("F3告警：身份切换后的迟到响应不得恢复旧列表", async () => {
  const context = await engine.newContext(), page = await context.newPage(); page.setDefaultTimeout(5000);
  let unblock, held;
  const ready = new Promise(resolve => { held = resolve; });
  const barrier = new Promise(resolve => { unblock = resolve; });
  try {
    await page.goto(profile.url + "/manage/alerts"); await identity(page, "alert_alice");
    await page.route("**/console/isolated-alerts", async route => { const response = await route.fetch(); held(); await barrier; await route.fulfill({ response }); }, { times: 1 });
    await page.getByRole("button", { name: "读取告警列表", exact: true }).click(); await ready;
    await identity(page, "owner_alice"); unblock();
    await page.getByRole("button", { name: "读取告警列表", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "403" }).waitFor();
    assert.equal(await page.getByRole("button", { name: "查看告警 synthetic-alert", exact: true }).count(), 0);
  } finally { unblock?.(); await context.close(); }
});
