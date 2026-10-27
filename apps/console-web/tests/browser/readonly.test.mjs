import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import { browser } from "./support.mjs";
import { control, event, held, hold, identity, inspect, mountReadOnly, releaseHeld, run, traceForm, traceId, traces } from "./readonly-fixture.mjs";
let engine;
before(async () => { engine = await browser(); });
after(async () => { await engine?.close(); });
function metric(page, label) { return page.locator("dt").filter({ hasText: new RegExp(`^${label}$`) }).locator("xpath=following-sibling::dd[1]"); }
async function runtime(route, row, events = [event(1), event(3)]) {
  if (row.path === `/v1/runs/${run.run_id}`) await route.fulfill({ json: run });
  else if (row.path.endsWith("/events")) await route.fulfill({ json: events });
  else if (row.path.endsWith("/stream")) await route.fulfill({ contentType: "text/event-stream", body: `event: done\ndata: ${JSON.stringify({ trace_id: traceId, url: "https://untrusted.invalid/" })}\n\n` });
  else return false;
  return true;
}

test("Operations：查询 UTC 时间窗与当前身份，未知不补零、USD 字符串不丢精度", async () => mountReadOnly(engine, "operations", async ({ page, calls, handle }) => {
  handle(async (route, row) => { await route.fulfill({ json: row.path.endsWith("/operations") ? control() : traces() }); return true; });
  await page.getByRole("button", { name: "刷新控制面", exact: true }).click();
  await metric(page, "Agent").filter({ hasText: /^7$/ }).waitFor();
  await traceForm(page);
  await page.getByRole("button", { name: "查询 Trace 指标", exact: true }).click();
  await metric(page, "Token 总量").waitFor();
  assert.equal(await metric(page, "Token 总量").innerText(), "未知");
  assert.equal(await metric(page, "费用总量（USD）").innerText(), "未知");
  assert.equal(await metric(page, "费用已知小计（USD）/ 费用完整 Trace").innerText(), "0.000000123456789 / 1");
  const query = calls.find(row => row.path.endsWith("/traces"));
  assert.equal(query.url.searchParams.get("since"), "2026-10-21T00:00:00.000Z");
  assert.equal(query.url.searchParams.get("until"), "2026-10-21T01:00:00.000Z");
  assert.equal(query.url.searchParams.get("agent_id"), "synthetic-agent");
  assert.equal(query.headers["x-console-tenant"], "default");
  assert.equal(query.headers.authorization, undefined);
  await page.getByLabel("Agent ID", { exact: true }).fill("changed-agent");
  assert.equal(await metric(page, "Token 总量").count(), 0);
}));

test("Operations：时间窗拒绝不请求，HTTP 与范围错误清空旧结果且不回显正文", async () => mountReadOnly(engine, "operations", async ({ page, calls, handle }) => {
  let failure = false, foreign = false;
  handle(async (route, row) => {
    if (failure) await route.fulfill({ status: 503, body: "synthetic-private-error-body" });
    else await route.fulfill({ json: row.path.endsWith("/operations") ? control(foreign ? "foreign" : "default") : traces() });
    return true;
  });
  await traceForm(page);
  await page.getByLabel("结束（浏览器本地时间）", { exact: true }).fill("2026-11-01T09:00");
  await page.getByRole("button", { name: "查询 Trace 指标", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "最长七天" }).waitFor();
  assert.equal(calls.length, 0);
  await traceForm(page);
  await page.getByRole("button", { name: "查询 Trace 指标", exact: true }).click();
  await metric(page, "Token 总量").waitFor();
  failure = true;
  await page.getByRole("button", { name: "查询 Trace 指标", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "HTTP 503" }).waitFor();
  assert.equal(await metric(page, "Token 总量").count(), 0);
  assert.equal((await page.locator("body").innerText()).includes("synthetic-private-error-body"), false);
  failure = false;
  await page.getByRole("button", { name: "刷新控制面", exact: true }).click();
  await metric(page, "Agent").waitFor();
  foreign = true;
  await page.getByRole("button", { name: "刷新控制面", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "控制面指标范围不匹配" }).waitFor();
  assert.equal(await metric(page, "Agent").count(), 0);
}));

test("Operations：忽略 abort 的旧控制面响应不得跨身份覆盖新结果", async () => mountReadOnly(engine, "operations", async ({ page, calls, handle }) => {
  handle(async (route, row) => { await route.fulfill({ json: control(row.headers["x-console-tenant"], row.headers["x-console-tenant"] === "other" ? 99 : 7) }); return true; });
  await hold(page);
  await page.getByRole("button", { name: "刷新控制面", exact: true }).click();
  await held(page);
  await page.getByLabel("租户", { exact: true }).fill("other");
  await page.evaluate(() => { window.syntheticTransport.hold = false; });
  await page.getByRole("button", { name: "刷新控制面", exact: true }).click();
  await metric(page, "Agent").filter({ hasText: /^99$/ }).waitFor();
  await releaseHeld(page);
  assert.equal(await metric(page, "Agent").innerText(), "99");
  assert.equal(await page.getByRole("alert").count(), 0);
  await page.getByRole("combobox", { name: /^角色/ }).selectOption("USER");
  assert.equal(await metric(page, "Agent").count(), 0);
  assert.equal(await page.getByRole("button", { name: "刷新控制面", exact: true }).isDisabled(), true);
  assert.equal(calls.length, 2);
}));

test("Operations：旧 Trace body 迟到不跨租户或 Agent 显示", async () => mountReadOnly(engine, "operations", async ({ page, handle }) => {
  handle(async (route, row) => {
    const next = row.url.searchParams.get("agent_id") === "next-agent";
    await route.fulfill({ json: next ? { ...traces(), tenant_id: "other", agent_id: "next-agent", tokens: { total: 777, known_trace_subtotal: 777, traces_with_complete_usage: 3 } } : traces() }); return true;
  });
  await traceForm(page); await hold(page);
  await page.getByRole("button", { name: "查询 Trace 指标", exact: true }).click(); await held(page);
  await page.getByLabel("租户", { exact: true }).fill("other");
  await page.getByLabel("Agent ID", { exact: true }).fill("next-agent");
  await page.evaluate(() => { window.syntheticTransport.hold = false; });
  await page.getByRole("button", { name: "查询 Trace 指标", exact: true }).click();
  await metric(page, "Token 总量").filter({ hasText: /^777$/ }).waitFor();
  await releaseHeld(page);
  assert.equal(await metric(page, "Token 总量").innerText(), "777");
  assert.equal(await page.getByRole("alert").count(), 0);
}));

test("Runs：身份仅内存、列表分页与筛选精确绑定，切换身份清空旧列表", async () => mountReadOnly(engine, "runs", async ({ page, calls, handle }) => {
  assert.equal(await page.getByRole("button", { name: "刷新 Run 列表", exact: true }).isDisabled(), true);
  handle(async route => { await route.fulfill({ json: { items: [{ ...run, run_id: `synthetic-${calls.length}` }], next_cursor: "opaque+/cursor" } }); return true; });
  await identity(page);
  await page.getByLabel("Agent 筛选", { exact: true }).fill("synthetic / agent");
  await page.getByRole("button", { name: "刷新 Run 列表", exact: true }).click();
  await page.getByRole("button", { name: "synthetic-1", exact: true }).waitFor();
  await page.getByRole("button", { name: "加载下一页", exact: true }).click();
  await page.getByRole("button", { name: "synthetic-2", exact: true }).waitFor();
  assert.equal(calls[1].url.searchParams.get("cursor"), "opaque+/cursor");
  assert.equal(calls[1].url.searchParams.get("agent_id"), "synthetic / agent");
  assert.equal(calls[1].headers.authorization, "Bearer synthetic-token");
  assert.equal(calls[1].headers["x-console-role"], undefined);
  await page.getByLabel("Agent 筛选", { exact: true }).fill("changed");
  await page.getByRole("button", { name: "加载下一页", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "筛选已变化" }).waitFor();
  assert.equal(calls.length, 2);
  await identity(page, "next-synthetic-token");
  assert.equal(await page.getByRole("button", { name: "synthetic-1", exact: true }).count(), 0);
}));

test("Runs：事件增量排序去重与 Trace 跳转仅指向配置的本机替身", async () => mountReadOnly(engine, "runs", async ({ page, context, calls, handle }) => {
  handle((route, row) => runtime(route, row, row.url.searchParams.get("since_seq") === "3" ? [event(3), event(5)] : [event(3), event(1)]));
  await context.route("http://127.0.0.1:3000/**", route => route.fulfill({ contentType: "text/html", body: "<h1>Synthetic Trace target</h1>" }));
  await identity(page); await inspect(page);
  const link = page.getByRole("link", { name: "在本机 Langfuse 查看 Trace", exact: true });
  await link.waitFor();
  assert.equal(await link.getAttribute("href"), `http://127.0.0.1:3000/project/synthetic-project/traces/${traceId}`);
  assert.equal(await link.getAttribute("rel"), "noopener noreferrer");
  assert.equal(await link.getAttribute("target"), "_blank");
  const popup = page.waitForEvent("popup"); await link.click();
  const target = await popup;
  await target.getByRole("heading", { name: "Synthetic Trace target", exact: true }).waitFor();
  assert.equal(target.url(), `http://127.0.0.1:3000/project/synthetic-project/traces/${traceId}`); await target.close();
  await page.getByRole("button", { name: "增量刷新轨迹", exact: true }).click();
  await page.getByText("#5 model.response", { exact: true }).waitFor();
  assert.deepEqual(await page.locator("ol > li > b").allTextContents(), ["#1 model.response", "#3 model.response", "#5 model.response"]);
  assert.equal(calls.filter(row => row.path.endsWith("/events"))[1].url.searchParams.get("since_seq"), "3");
}));

test("Runs：401／403／404 清空旧轨迹，非法事件与非法 Trace 不显示", async () => mountReadOnly(engine, "runs", async ({ page, handle }) => {
  let status = 200, invalid = false, invalidTrace = false;
  handle(async (route, row) => {
    if (status !== 200) { await route.fulfill({ status, body: "synthetic-private-runtime-body" }); return true; }
    if (row.path.endsWith("/stream") && invalidTrace) { await route.fulfill({ contentType: "text/event-stream", body: 'event: done\ndata: {"trace_id":"https://untrusted.invalid/","url":"https://untrusted.invalid/"}\n\n' }); return true; }
    return runtime(route, row, invalid ? [{ ...event(1), run_id: "foreign-run" }] : [event(1)]);
  });
  await identity(page);
  for (const denied of [401, 403, 404]) {
    status = 200; await inspect(page);
    await page.getByRole("link", { name: "在本机 Langfuse 查看 Trace", exact: true }).waitFor();
    status = denied;
    await page.getByRole("button", { name: "重新完整读取", exact: true }).click();
    await page.getByRole("status").filter({ hasText: `Runtime HTTP ${denied}` }).waitFor();
    assert.equal(await page.getByRole("heading", { name: `${run.run_id} · completed`, exact: true }).count(), 0);
    assert.equal(await page.getByRole("link").count(), 0);
    assert.equal((await page.locator("body").innerText()).includes("synthetic-private-runtime-body"), false);
  }
  status = 200; invalid = true; await inspect(page);
  await page.getByRole("status").filter({ hasText: "事件响应不符合 Run 契约" }).waitFor();
  assert.equal(await page.locator("ol > li").count(), 0);
  invalid = false; invalidTrace = true; await inspect(page);
  await page.getByRole("heading", { name: `${run.run_id} · completed`, exact: true }).waitFor();
  assert.equal(await page.getByRole("link").count(), 0);
}));

test("Runs：读取期间锁定身份，卸载后迟到 body 不得继续请求或污染重新挂载页", async () => mountReadOnly(engine, "runs", async ({ page, calls, handle }) => {
  handle((route, row) => runtime(route, row));
  await identity(page); await hold(page); await inspect(page); await held(page);
  assert.equal(await page.getByRole("button", { name: "应用读取身份并清空旧数据", exact: true }).isDisabled(), true);
  await page.locator("#unmount").click(); await page.locator("#remount").click();
  await identity(page, "next-synthetic-token");
  await releaseHeld(page);
  assert.equal(await page.evaluate(() => window.syntheticTransport.paths.length), 1, "Stale read must not dispatch a follow-up fetch");
  assert.equal(calls.length, 1, "Unmounted read must not continue to events or stream with cleared credentials");
  assert.equal(await page.getByRole("heading", { name: `${run.run_id} · completed`, exact: true }).count(), 0);
  assert.equal(await page.getByRole("link").count(), 0);
}));

test("Runs：拒绝不匹配的 Run 元数据，不请求其事件或 Trace", async () => mountReadOnly(engine, "runs", async ({ page, calls, handle }) => {
  handle(async (route, row) => {
    if (row.path === `/v1/runs/${run.run_id}`) { await route.fulfill({ json: { ...run, run_id: "foreign-run" } }); return true; }
    return runtime(route, row);
  });
  await identity(page); await inspect(page);
  await page.getByRole("status").filter({ hasText: "Run 响应范围不匹配" }).waitFor();
  assert.equal(calls.length, 1);
  assert.equal(await page.getByRole("heading", { name: "foreign-run · completed", exact: true }).count(), 0);
}));
