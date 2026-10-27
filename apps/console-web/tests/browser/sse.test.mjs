import { before, after, test } from "node:test";
import assert from "node:assert/strict";
import { apply, browser, profile, mockLedgerRead, ledgerIdentity } from "./support.mjs";
let engine;
before(async () => { engine = await browser(); });
after(async () => { await engine?.close(); });
async function workbench(action, initial = null) {
  let active = initial;
  const context = await engine.newContext();
  await context.addInitScript(() => {
    const nativeFetch = window.fetch.bind(window);
    window.sseFixture = { hold: false, streams: [], mutations: [] };
    window.fetch = async (url, options = {}) => {
      const path = String(url), fixture = window.sseFixture;
      if (options.method && options.method !== "GET") fixture.mutations.push(path);
      if (!path.includes("/stream?")) return nativeFetch(url, options);
      const entry = { path, headers: options.headers, canceled: false };
      fixture.streams.push(entry);
      const body = new ReadableStream({ start(controller) { entry.controller = controller; }, cancel() { entry.canceled = true; } });
      if (fixture.hold) await new Promise(resolve => { entry.release = resolve; });
      return new Response(body, { headers: { "content-type": "text/event-stream" } });
    };
  });
  await context.route("**/v1/**", async route => {
    if (initial && route.request().method() === "GET") {
      const path = new URL(route.request().url()).pathname;
      const other = route.request().headers().authorization === "Bearer synthetic-bob";
      const identity = other ? { ...ledgerIdentity, subject: "human:bob", scope_id: "c".repeat(64) } : ledgerIdentity;
      if (path === "/v1/workbench/identity") return route.fulfill({ json: identity });
      if (path === "/v1/workbench/attempts") return route.fulfill({ json: { scope_id: identity.scope_id, active: other ? null : active } });
      if (path === "/v1/runs/run1") return route.fulfill({ json: { run_id: "run1", agent_id: "demo", subject: "human:alice", state: "completed", last_seq: 5 } });
      if (path === "/v1/runs/run1/events") return route.fulfill({ json: [] });
    }
    if (!await mockLedgerRead(route)) await route.fulfill({ status: 405 });
  });
  const page = await context.newPage(); page.setDefaultTimeout(5000);
  try {
    await page.goto(`${profile.url}/tests/browser/workbench.html`); await apply(page, "synthetic-alice");
    await page.getByLabel("Run ID", { exact: true }).fill("run1");
    await action(page, value => { active = value; });
    assert.deepEqual(await page.evaluate(() => window.sseFixture.mutations), []);
  } finally { await context.close(); }
}
async function start(page, index = 0) {
  await page.getByRole("button", { name: "跟随／续读已持久化事件", exact: true }).click();
  await page.waitForFunction(index => window.sseFixture.streams.length > index, index);
}
async function push(page, index, seq, text, runId = "run1") {
  await page.evaluate(({ index, seq, text, runId }) => {
    const value = { run_id: runId, seq, type: "model.response", payload: { text } };
    window.sseFixture.streams[index].controller.enqueue(new TextEncoder().encode(`event: model.response\nid: ${seq}\ndata: ${JSON.stringify(value)}\n\n`));
  }, { index, seq, text, runId });
}
test("SSE DOM：断连后只手动按同Run游标续读；done不推断完成，无写请求", async () => workbench(async page => {
  await start(page); await push(page, 0, 1, "第一条已持久化回答");
  await page.getByText("1 · model.response · 第一条已持久化回答", { exact: true }).waitFor();
  await page.evaluate(() => window.sseFixture.streams[0].controller.close());
  await page.getByRole("status").filter({ hasText: "跟随中断" }).waitFor();
  assert.equal(await page.evaluate(() => window.sseFixture.streams.length), 1);
  await start(page, 1);
  assert.equal(await page.evaluate(() => window.sseFixture.streams[1].path), "/v1/runs/run1/stream?since_seq=1");
  await push(page, 1, 3, "续读已持久化回答");
  await page.evaluate(() => window.sseFixture.streams[1].controller.enqueue(new TextEncoder().encode('event: done\ndata: {"type":"done","run_id":"run1","stop_reason":"awaiting"}\n\n')));
  await page.getByRole("status").filter({ hasText: "不推断 Run 完成" }).waitFor();
  assert.equal(await page.locator('section[aria-label="已持久化事件跟随"] li').count(), 2);
  assert.equal(await page.getByRole("button", { name: "预览批准", exact: true }).count(), 0);
}));
test("SSE DOM：跟随中切身份清空游标；忽略abort的旧响应迟到不能污染新身份", async () => workbench(async page => {
  await page.evaluate(() => { window.sseFixture.hold = true; }); await start(page);
  await push(page, 0, 1, "旧身份不可显示");
  await apply(page, "synthetic-bob");
  assert.equal(await page.getByLabel("Run ID", { exact: true }).inputValue(), "");
  assert.equal(await page.getByRole("region", { name: "已持久化事件跟随" }).count(), 0);
  await page.evaluate(() => { window.sseFixture.hold = false; });
  await page.getByLabel("Run ID", { exact: true }).fill("run1"); await start(page, 1);
  await push(page, 1, 2, "新身份已持久化回答");
  await page.getByText("2 · model.response · 新身份已持久化回答", { exact: true }).waitFor();
  await page.evaluate(() => window.sseFixture.streams[0].release());
  await page.waitForFunction(() => window.sseFixture.streams[0].canceled);
  assert.equal(await page.getByText(/旧身份不可显示/).count(), 0);
  assert.equal(await page.evaluate(() => window.sseFixture.streams[1].headers.Authorization), "Bearer synthetic-bob");
  assert.equal(await page.evaluate(() => window.sseFixture.streams[1].path), "/v1/runs/run1/stream?since_seq=0");
  await page.getByRole("button", { name: "停止本次只读刷新", exact: true }).click();
}));
test("SSE DOM：停止取消挂起读取；卸载后的迟到响应释放且不追加请求", async () => workbench(async page => {
  await start(page); await push(page, 0, 1, "停止前已确认");
  await page.getByText("1 · model.response · 停止前已确认", { exact: true }).waitFor();
  await page.getByRole("button", { name: "停止本次只读刷新", exact: true }).click();
  await page.waitForFunction(() => window.sseFixture.streams[0].canceled);
  await page.getByRole("status").filter({ hasText: "未取消 Run" }).waitFor();
  await page.evaluate(() => { window.sseFixture.hold = true; }); await start(page, 1);
  assert.equal(await page.evaluate(() => window.sseFixture.streams[1].path), "/v1/runs/run1/stream?since_seq=1");
  await page.locator("#unmount").click();
  await push(page, 1, 2, "卸载后不可显示");
  await page.evaluate(() => window.sseFixture.streams[1].release());
  await page.waitForFunction(() => window.sseFixture.streams[1].canceled);
  assert.equal(await page.evaluate(() => window.sseFixture.streams.length), 2);
  assert.equal(await page.getByRole("region", { name: "已持久化事件跟随" }).count(), 0);
}));
test("SSE DOM：跨Run坏帧不推进游标、不显示正文或卡片", async () => workbench(async page => {
  await start(page); await push(page, 0, 1, "foreign-sensitive", "foreign");
  await page.getByRole("status").filter({ hasText: "跟随中断" }).waitFor();
  await page.getByRole("heading", { name: "只读跟随 run1 · 已确认序号 0", exact: true }).waitFor();
  assert.equal(await page.getByText(/foreign-sensitive/).count(), 0);
  await page.waitForFunction(() => window.sseFixture.streams[0].canceled);
}));

const active = { scope_id: ledgerIdentity.scope_id, attempt_id: "11111111-1111-4111-8111-111111111111", run_id: "run1",
  action: "credential", request_seq: 4, fingerprint: "b".repeat(64), status: "COMMITTED", revision: 2,
  evidence_seq: 5, rejection: null, acknowledged: false };
const prepared = { ...active, status: "PREPARED", revision: 1, evidence_seq: null };
const newWrite = page => page.getByRole("button", { name: "登记新 Run（尚未创建）", exact: true });
test("SSE与写锁：断连续读、done、Run终态及空账本都不代替本人收口", async () => workbench(async (page, setActive) => {
  assert.equal(await newWrite(page).isDisabled(), true);
  await start(page); await push(page, 0, 5, "已提交但未收口");
  await page.getByText("5 · model.response · 已提交但未收口", { exact: true }).waitFor();
  await page.evaluate(() => window.sseFixture.streams[0].controller.close());
  await page.getByRole("status").filter({ hasText: "跟随中断" }).waitFor();
  assert.equal(await newWrite(page).isDisabled(), true);
  await start(page, 1);
  assert.equal(await page.evaluate(() => window.sseFixture.streams[1].path), "/v1/runs/run1/stream?since_seq=5");
  await page.evaluate(() => window.sseFixture.streams[1].controller.enqueue(new TextEncoder().encode('event: done\ndata: {"type":"done","run_id":"run1","stop_reason":"end_turn"}\n\n')));
  await page.getByRole("status").filter({ hasText: "不推断 Run 完成" }).waitFor();
  await page.getByRole("button", { name: "读取 Run 与待办卡片", exact: true }).click();
  await page.getByRole("heading", { name: "run1 · completed · 发起人 human:alice", exact: true }).waitFor();
  assert.equal(await newWrite(page).isDisabled(), true);
  setActive(null);
  await page.getByRole("button", { name: "只读核对写账本", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "保持写锁" }).waitFor();
  assert.equal(await newWrite(page).isDisabled(), true);
  setActive(active);
  await page.getByRole("button", { name: "只读核对写账本", exact: true }).click();
  await page.getByRole("button", { name: "预览本人确认收口", exact: true }).waitFor({ state: "visible" });
  await page.waitForFunction(() => ![...document.querySelectorAll("button")].find(button => button.textContent === "预览本人确认收口")?.disabled);
  assert.equal(await newWrite(page).isDisabled(), true);
}, active));

test("SSE与写锁：换主体清旧流，同主体换令牌恢复原锁，停止读取不解锁", async () => workbench(async page => {
  await page.evaluate(() => { window.sseFixture.hold = true; });
  await start(page); await push(page, 0, 5, "Alice旧流不可跨身份");
  await apply(page, "synthetic-bob");
  assert.equal(await newWrite(page).isDisabled(), false);
  await page.getByText(`synthetic / human:bob · ${"c".repeat(64)}`, { exact: true }).waitFor();
  await apply(page, "synthetic-alice-fresh");
  assert.equal(await newWrite(page).isDisabled(), true);
  await page.getByRole("button", { name: "确认执行已登记凭据刷新一次", exact: true }).waitFor();
  await page.evaluate(() => { window.sseFixture.hold = false; window.sseFixture.streams[0].release(); });
  await page.waitForFunction(() => window.sseFixture.streams[0].canceled);
  assert.equal(await page.getByText(/Alice旧流不可跨身份/).count(), 0);
  await page.getByLabel("Run ID", { exact: true }).fill("run1");
  await start(page, 1);
  assert.equal(await page.evaluate(() => window.sseFixture.streams[1].path), "/v1/runs/run1/stream?since_seq=0");
  assert.equal(await page.evaluate(() => window.sseFixture.streams[1].headers.Authorization), "Bearer synthetic-alice-fresh");
  await page.getByRole("button", { name: "停止本次只读刷新", exact: true }).click();
  await page.waitForFunction(() => window.sseFixture.streams[1].canceled);
  assert.equal(await newWrite(page).isDisabled(), true);
}, prepared));

test("SSE与写锁：卸载释放迟到流，重载只读恢复未收口账本，不执行或ACK", async () => workbench(async page => {
  await page.evaluate(() => { window.sseFixture.hold = true; });
  await start(page);
  await page.locator("#unmount").click();
  await push(page, 0, 5, "卸载后不得显示的写锁相关事件");
  await page.evaluate(() => window.sseFixture.streams[0].release());
  await page.waitForFunction(() => window.sseFixture.streams[0].canceled);
  assert.equal(await page.getByRole("region", { name: "活动写回执" }).count(), 0);
  assert.equal(await page.getByText(/卸载后不得显示/).count(), 0);
  assert.deepEqual(await page.evaluate(() => window.sseFixture.mutations), []);
  await page.reload(); await apply(page, "synthetic-alice-fresh");
  await page.getByRole("button", { name: "预览本人确认收口", exact: true }).waitFor();
  assert.equal(await newWrite(page).isDisabled(), true);
  assert.equal(await page.getByRole("region", { name: "已持久化事件跟随" }).count(), 0);
  assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
}, active));
