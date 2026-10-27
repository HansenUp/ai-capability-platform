import { before, after, test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { browser, profile } from "./support.mjs";

let engine, tokens;
before(async () => { engine = await browser(); tokens = JSON.parse(await readFile(profile.tokens, "utf8")); });
after(async () => { await engine?.close(); });
async function identity(page, name) {
  await page.getByLabel("Owner用户令牌", { exact: true }).fill(tokens[name]);
  await page.getByRole("button", { name: "应用Owner身份并清空", exact: true }).click();
  assert.equal(await page.getByLabel("Owner用户令牌", { exact: true }).inputValue(), "");
}
async function open(run) {
  const context = await engine.newContext({ timezoneId: "UTC" });
  const page = await context.newPage(), calls = [];
  page.on("request", request => { if (request.url().includes("/console/owner/")) calls.push(request); });
  try {
    await page.goto(profile.url + "/manage/owner-operations");
    await page.getByRole("heading", { name: "Owner单Agent只读运营", exact: true }).waitFor();
    await page.getByLabel("Owner Agent ID", { exact: true }).fill("s12-owner");
    await identity(page, "owner_alice");
    await run(page, calls);
    assert.equal(calls.every(request => request.method() === "GET"), true);
    assert.equal(calls.every(request => !Object.keys(request.headers()).some(name => name.startsWith("x-console-"))), true);
    assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
  } finally { await context.close(); }
}

test("Owner：真实隔离Console验签／分配／接手前Trace聚合，换主体拒绝并清空", async () => open(async page => {
  await page.getByRole("button", { name: "查询Owner摘要", exact: true }).click();
  await page.getByRole("heading", { name: "Owner摘要", exact: true }).waitFor();
  await page.getByLabel("Owner开始时间", { exact: true }).fill("2026-10-21T00:00");
  await page.getByLabel("Owner结束时间", { exact: true }).fill("2026-10-22T00:00");
  assert.equal(await page.getByRole("heading", { name: "Owner摘要", exact: true }).count(), 0);
  await page.getByRole("button", { name: "查询Owner摘要", exact: true }).click();
  await page.getByRole("heading", { name: "Owner摘要", exact: true }).waitFor();
  await page.getByRole("button", { name: "查询Owner Trace", exact: true }).click();
  await page.getByRole("heading", { name: "Owner Trace聚合", exact: true }).waitFor();
  assert.equal(await page.locator("dt").filter({ hasText: /^Token总量$/ }).locator("xpath=following-sibling::dd[1]").textContent(), "9");
  await identity(page, "owner_bob");
  assert.equal(await page.getByRole("heading", { name: "Owner摘要", exact: true }).count(), 0);
  assert.equal(await page.getByRole("heading", { name: "Owner Trace聚合", exact: true }).count(), 0);
  await page.getByRole("button", { name: "查询Owner摘要", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "HTTP 404" }).waitFor();
  assert.equal(await page.getByRole("button", { name: "查询Owner Trace", exact: true }).isDisabled(), true);
}));

test("Owner：旧身份忽略abort的迟到响应不能恢复数据，错误不显示旧摘要", async () => open(async page => {
  await page.addScriptTag({ content: `window.ownerHeld = null; const native = window.fetch; window.fetch = async (input, init) => {
    if (String(input).includes('/console/owner/') && !window.ownerCaptured) {
      window.ownerCaptured = true; const response = await native(input, { ...init, signal: undefined });
      return new Promise(resolve => { window.ownerHeld = () => resolve(response); });
    } return native(input, init);
  };` });
  await page.getByRole("button", { name: "查询Owner摘要", exact: true }).click();
  await page.waitForFunction(() => !!window.ownerHeld);
  await identity(page, "owner_bob");
  await page.evaluate(() => window.ownerHeld());
  await page.getByRole("button", { name: "查询Owner摘要", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "HTTP 404" }).waitFor();
  assert.equal(await page.getByRole("heading", { name: "Owner摘要", exact: true }).count(), 0);
}));

test("Owner：Trace缺失值未知、USD字符串不转数值，非法用量拒收并清空", async () => open(async page => {
  let mode = "missing";
  await page.route("**/console/owner/**/trace-metrics?*", async route => {
    const response = await route.fetch(), body = await response.json();
    if (mode === "missing") {
      body.tokens = { total: null, known_trace_subtotal: 0, traces_with_complete_usage: 0 };
      body.cost_usd = { total: null, known_trace_subtotal: "0", traces_with_complete_cost: 0 };
    } else if (mode === "scientific") {
      body.cost_usd.total = body.cost_usd.known_trace_subtotal = "1E-7";
    } else body.tokens.total = Number.MAX_SAFE_INTEGER + 1;
    await route.fulfill({ json: body });
  });
  await page.getByLabel("Owner开始时间", { exact: true }).fill("2026-10-21T00:00");
  await page.getByLabel("Owner结束时间", { exact: true }).fill("2026-10-22T00:00");
  await page.getByRole("button", { name: "查询Owner Trace", exact: true }).click();
  const metric = label => page.locator("dt").filter({ hasText: new RegExp(`^${label}$`) }).locator("xpath=following-sibling::dd[1]");
  await page.getByRole("heading", { name: "Owner Trace聚合", exact: true }).waitFor();
  assert.equal(await metric("Token总量").textContent(), "未知");
  assert.equal(await metric("来源费用USD").textContent(), "未知");
  mode = "scientific";
  await page.getByRole("button", { name: "查询Owner Trace", exact: true }).click();
  await metric("来源费用USD").filter({ hasText: /^1E-7$/ }).waitFor();
  mode = "bad";
  await page.getByRole("button", { name: "查询Owner Trace", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "Owner指标无效" }).waitFor();
  assert.equal(await page.getByRole("heading", { name: "Owner Trace聚合", exact: true }).count(), 0);
}));

test("Owner：503清空对应旧摘要且不回显正文，卸载不接纳迟到响应", async () => open(async page => {
  await page.getByRole("button", { name: "查询Owner摘要", exact: true }).click();
  await page.getByRole("heading", { name: "Owner摘要", exact: true }).waitFor();
  await page.route("**/console/owner/**/summary", route => route.fulfill({ status: 503, body: "PRIVATE-DIAGNOSTIC" }));
  await page.getByRole("button", { name: "查询Owner摘要", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "HTTP 503" }).waitFor();
  assert.equal(await page.getByText("PRIVATE-DIAGNOSTIC").count(), 0);
  assert.equal(await page.getByRole("heading", { name: "Owner摘要", exact: true }).count(), 0);
  await page.unroute("**/console/owner/**/summary");
  await page.addScriptTag({ content: `window.ownerHeld = null; const native = window.fetch; window.fetch = async (input, init) => {
    if (String(input).includes('/console/owner/')) {
      const response = await native(input, { ...init, signal: undefined });
      return new Promise(resolve => { window.ownerHeld = () => resolve(response); });
    } return native(input, init);
  };` });
  await page.getByRole("button", { name: "查询Owner摘要", exact: true }).click();
  await page.waitForFunction(() => !!window.ownerHeld);
  await page.locator("a[href='/chat']").click();
  await page.getByRole("heading", { name: "Owner单Agent只读运营", exact: true }).waitFor({ state: "detached" });
  await page.evaluate(() => window.ownerHeld());
  await page.locator("a[href='/manage/owner-operations']").click();
  await page.getByRole("heading", { name: "Owner单Agent只读运营", exact: true }).waitFor();
  assert.equal(await page.getByRole("heading", { name: "Owner摘要", exact: true }).count(), 0);
  assert.equal(await page.getByRole("button", { name: "查询Owner摘要", exact: true }).isDisabled(), true);
}));
