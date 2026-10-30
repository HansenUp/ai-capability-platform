import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import { browser } from "./support.mjs";
import { flag, mount, release, version } from "./management-fixture.mjs";
let engine;
before(async () => { engine = await browser(); });
after(async () => { await engine?.close(); });

test("发布组件：开发闸门显式确认、取消不写入、确认仅一次且服从服务端拒绝", async () => mount(engine, "agents", async ({ page, writes, handle }) => {
  await release(page);
  await page.getByRole("button", { name: "检查并发布", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "须显式确认" }).waitFor();
  assert.equal(writes().length, 0);
  await page.getByLabel("显式确认开发态评测通过（非真实评测验收）", { exact: true }).check();
  await page.getByRole("button", { name: "检查并发布", exact: true }).click();
  assert.equal(await page.getByRole("button", { name: "应用开发身份", exact: true }).isDisabled(), true);
  await page.getByRole("button", { name: "取消变更", exact: true }).click();
  assert.equal(writes().length, 0);
  handle(async (route, row) => {
    if (row.method === "GET") return false;
    await route.fulfill({ status: 409, json: { detail: "synthetic evaluation gate rejected" } }); return true;
  });
  await page.getByRole("button", { name: "检查并发布", exact: true }).click();
  await page.getByRole("button", { name: "确认执行上述变更", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "409" }).waitFor();
  assert.equal(writes().length, 1);
  assert.equal(writes()[0].path, "/agents/synthetic-agent/versions/1/publish");
  assert.deepEqual(writes()[0].body, { eval_run_ref: null, eval_passed: true, canary_percent: 100, canary_allowlist: [] });
  assert.equal(writes()[0].headers["x-console-tenant"], "default");
  assert.match(await page.getByRole("row").filter({ hasText: version.agent_id }).innerText(), /draft/);
}));

test("发布组件：0% 必须白名单，去重参数绑定确认快照，成功后刷新服务端", async () => mount(engine, "agents", async ({ page, writes, handle }) => {
  await release(page);
  await page.getByLabel("灰度比例", { exact: true }).fill("0");
  await page.getByRole("button", { name: "检查并发布", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "必须有评测白名单" }).waitFor();
  await page.getByLabel("灰度白名单（每行或逗号分隔）", { exact: true }).fill("human:alice, human:alice\nhuman:bob");
  await page.getByRole("button", { name: "检查并发布", exact: true }).click();
  assert.equal(writes().length, 0);
  assert.equal(await page.getByLabel("灰度比例", { exact: true }).isDisabled(), true);
  let saved = false;
  handle(async (route, row) => {
    if (row.method === "POST") { saved = true; await route.fulfill({ json: {} }); return true; }
    if (saved && row.path === "/agents") { await route.fulfill({ json: [{ ...version, status: "published", canary: { percent: 0, allowlist: ["human:alice", "human:bob"] } }] }); return true; }
    return false;
  });
  await page.getByRole("button", { name: "确认执行上述变更", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "操作成功" }).waitFor();
  await page.getByText(/刷新不会重发变更/).waitFor();
  assert.deepEqual(writes()[0].body, { eval_run_ref: null, eval_passed: false, canary_percent: 0, canary_allowlist: ["human:alice", "human:bob"] });
  assert.equal(writes().length, 1);
  assert.match(await page.getByRole("row").filter({ hasText: version.agent_id }).innerText(), /published/);
}));

test("发布组件：真实模式引用校验、只读评测未知不补零、引用与身份切换清空旧结果", async () => mount(engine, "agents", async ({ page, calls, writes, handle }) => {
  const policy = { ...flag, mode: "langfuse", datasets: ["synthetic-dataset"] };
  handle(async (route, row) => {
    if (row.path.endsWith("eval-gate")) { await route.fulfill({ json: policy }); return true; }
    if (row.path.includes("/evaluation")) {
      await route.fulfill({ json: { status: "unavailable", reason: "synthetic unavailable", passed_items: null, total_items: null, pass_rate: null,
        source: "langfuse", checked_at: "2026-10-21T00:00:00Z", eval_run_ref: "langfuse:synthetic/run", stored_eval_run_ref: null, policy } }); return true;
    }
    if (row.path === "/agents" && row.headers["x-console-tenant"] === "other") { await route.fulfill({ json: [] }); return true; }
    return false;
  });
  await release(page);
  await page.getByRole("button", { name: "检查并发布", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "真实 Langfuse" }).waitFor();
  await page.getByLabel("真实评测引用", { exact: true }).fill("langfuse:synthetic/run");
  await page.getByRole("button", { name: "只读重算评测结果", exact: true }).click();
  const result = page.getByRole("region", { name: "评测结果只读视图", exact: true });
  await result.waitFor();
  assert.match(await result.innerText(), /通过条目 未知 \/ 总条目 未知 · 通过率 未知/);
  assert.equal(calls.find(row => row.path.endsWith("/evaluation")).url.searchParams.get("eval_run_ref"), "langfuse:synthetic/run");
  await page.getByLabel("真实评测引用", { exact: true }).fill("langfuse:changed/run");
  assert.equal(await result.count(), 0);
  await page.getByLabel("租户", { exact: true }).fill("other");
  const changed = page.waitForResponse(response => response.url().endsWith("/console/agents") && response.request().headers()["x-console-tenant"] === "other");
  await page.getByRole("button", { name: "应用开发身份", exact: true }).click();
  await changed;
  assert.equal(await page.getByRole("button", { name: "检查并发布", exact: true }).count(), 0);
  assert.equal(writes().length, 0);
}));

test("发布组件：已发布快照只能复制新版本，USER 不可保存或发布", async () => mount(engine, "agents", async ({ page, writes, handle }) => {
  handle(async (route, row) => {
    if (row.path === "/agents") { await route.fulfill({ json: [{ ...version, status: "published" }] }); return true; }
    return false;
  });
  await page.getByRole("button", { name: "刷新版本", exact: true }).click();
  await page.getByRole("button", { name: "复制为新草稿", exact: true }).click();
  assert.equal(await page.getByLabel("版本", { exact: true }).inputValue(), "");
  assert.deepEqual(JSON.parse(await page.getByLabel("完整 config JSON（高级编辑，保留扩展字段）", { exact: true }).inputValue()), version.config);
  await page.getByRole("combobox", { name: /^角色/ }).selectOption("USER");
  await page.getByRole("button", { name: "应用开发身份", exact: true }).click();
  assert.equal(await page.getByRole("button", { name: "仅保存草稿", exact: true }).isDisabled(), true);
  assert.equal(await page.getByRole("button", { name: "评测与发布", exact: true }).isDisabled(), true);
  assert.equal(writes().length, 0);
}));

test("发布组件：灰度 PUT 与回滚 POST 分离，回滚取消不写入", async () => mount(engine, "agents", async ({ page, writes, handle }) => {
  handle(async (route, row) => {
    if (row.path === "/agents") { await route.fulfill({ json: [{ ...version, status: "published", canary: { percent: 20, allowlist: [] } }] }); return true; }
    if (row.method !== "GET") { await route.fulfill({ json: {} }); return true; }
    return false;
  });
  await page.getByRole("button", { name: "刷新版本", exact: true }).click();
  await release(page);
  await page.getByLabel("灰度比例", { exact: true }).fill("10");
  await page.getByRole("button", { name: "检查并调整灰度", exact: true }).click();
  assert.equal(writes().length, 0);
  await page.getByRole("button", { name: "确认执行上述变更", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "操作成功" }).waitFor();
  await page.getByText(/刷新不会重发变更/).waitFor();
  assert.deepEqual([writes()[0].method, writes()[0].path, writes()[0].body], ["PUT", "/agents/synthetic-agent/versions/1/canary", { percent: 10, allowlist: [] }]);
  await page.getByRole("button", { name: "检查回滚", exact: true }).click();
  await page.getByRole("button", { name: "取消变更", exact: true }).click();
  assert.equal(writes().length, 1);
  await page.getByRole("button", { name: "检查回滚", exact: true }).click();
  const response = page.waitForResponse(value => value.url().endsWith("/rollback"));
  await page.getByRole("button", { name: "确认执行上述变更", exact: true }).click();
  await response;
  assert.deepEqual([writes()[1].method, writes()[1].path, writes()[1].body], ["POST", "/agents/synthetic-agent/versions/1/rollback", null]);
  assert.equal(writes().length, 2);
}));

test("F3：评测处理指引区分失败与通过，始终只读", async () => mount(engine, "agents", async ({ page, writes, handle }) => {
  const policy = { ...flag, mode: "langfuse", min_items: 10, min_pass_rate: 0.9 };
  let status = "failed";
  handle(async (route, row) => {
    if (row.path.endsWith("/eval-gate")) { await route.fulfill({ json: policy }); return true; }
    if (row.path.endsWith("/evaluation")) {
      const unavailable = status === "unavailable", passed = status === "passed";
      await route.fulfill({ json: { status, reason: passed ? null : "synthetic reason", passed_items: unavailable ? null : passed ? 10 : 1,
        total_items: unavailable ? null : passed ? 10 : 2, pass_rate: unavailable ? null : passed ? 1 : 0.5,
        source: "langfuse", checked_at: "2026-10-29T00:00:00Z", eval_run_ref: "langfuse:synthetic/run", stored_eval_run_ref: null, policy } }); return true;
    }
    return false;
  });
  await release(page);
  await page.getByLabel("真实评测引用", { exact: true }).fill("langfuse:synthetic/run");
  const read = page.getByRole("button", { name: "只读重算评测结果", exact: true });
  await read.click(); await page.getByText(/样本不足：当前 2 条/).waitFor();
  await page.getByText(/通过率未达当前策略：/).waitFor();
  status = "unavailable"; await read.click(); await page.getByText(/评测证据暂不可核验/).waitFor();
  assert.equal(await page.getByText(/样本不足：当前/).count(), 0);
  status = "passed"; await read.click(); await page.getByText(/本次只读重算满足当前策略/).waitFor();
  assert.equal(writes().length, 0);
  await page.getByLabel("真实评测引用", { exact: true }).fill("langfuse:other/run");
  assert.equal(await page.getByRole("region", { name: "评测处理指引" }).count(), 0);
}));

test("F11：同版本只读结果对照保留未知，切换身份清空，不产生写请求", async () => mount(engine, "agents", async ({page,writes,handle}) => {
 const policy={...flag,mode:"langfuse",min_items:10,min_pass_rate:0.9}; let available=false;
 handle(async(route,row)=>{
  if(row.path.endsWith("eval-gate")){await route.fulfill({json:policy});return true;}
  if(row.path.endsWith("/evaluation")){await route.fulfill({json:{status:available?"passed":"unavailable",reason:available?null:"unavailable",passed_items:available?10:null,total_items:available?10:null,pass_rate:available?1:null,source:"synthetic",checked_at:available?"2026-10-30T01:00:00Z":"2026-10-30T00:00:00Z",eval_run_ref:available?"langfuse:synthetic/new":"langfuse:synthetic/old",stored_eval_run_ref:null,policy}});return true;}
  return false;
 });
 await release(page);
 const read=page.getByRole("button",{name:"只读重算评测结果",exact:true});
 await read.click();await page.getByRole("button",{name:"保留当前结果作只读对照"}).click();
 available=true;await page.getByLabel("真实评测引用",{exact:true}).fill("langfuse:synthetic/new");
 assert.equal(await page.getByRole("region",{name:"评测结果并排对照"}).count(),0);
 await read.click();const comparison=page.getByRole("region",{name:"评测结果并排对照"});await comparison.waitFor();
 assert.ok((await comparison.innerText()).includes("未知 / 未知"));assert.ok((await comparison.innerText()).includes("10 / 10"));
 assert.match(await comparison.innerText(),/不能据此判断质量提升/);assert.equal(writes().length,0);
 await page.getByRole("button",{name:"清除对照结果"}).click();assert.equal(await comparison.count(),0);
 await page.getByRole("button",{name:"保留当前结果作只读对照"}).click();
 await page.getByRole("button",{name:"应用开发身份",exact:true}).click();assert.equal(await comparison.count(),0);
}));
