import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import { browser, profile } from "./support.mjs";
import { readFile } from "node:fs/promises";
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
test("F11：读取失败清空结果，无样本不冒充通过，导航不写入", async () => mount(engine, "agents", async ({page,writes,handle}) => {
  let fail = false;
  handle(async (route, row) => {
    if (!row.path.endsWith("/evaluation")) return false;
    await route.fulfill(fail ? {status:503,json:{detail:"synthetic unavailable"}} : {json:{status:"failed",reason:null,passed_items:0,total_items:0,pass_rate:null,source:"synthetic",checked_at:"2026-10-31T00:00:00Z",eval_run_ref:null,stored_eval_run_ref:null,policy:{...flag,mode:"langfuse",min_items:10}}});
    return true;
  });
  await release(page);
  await page.getByRole("navigation", {name:"评测流程导航"}).waitFor();
  await page.getByText(/尚未读取当前引用的评测证据/).waitFor();
  const read = page.getByRole("button",{name:"只读重算评测结果",exact:true});
  await read.click();
  await page.getByText(/未提供原因，不能据此判断通过/).waitFor();
  await page.getByText(/样本不足：当前 0 条/).waitFor();
  fail = true;
  await read.click();
  await page.getByRole("alert").filter({hasText:"读取失败，结果未知"}).waitFor();
  assert.equal(await page.getByRole("region",{name:"评测结果只读视图",exact:true}).count(),0);
  assert.equal(writes().length,0);
}));

test("F11：只读请求在途切换身份，迟到评测不得恢复旧结果", async () => mount(engine, "agents", async ({page,writes,handle}) => {
  let finish;
  const held = new Promise(resolve => { finish = resolve; });
  let started;
  const arrived = new Promise(resolve => { started = resolve; });
  handle(async (route,row) => {
    if (!row.path.endsWith("/evaluation")) return false;
    started(); await held;
    await route.fulfill({json:{status:"passed",reason:"OLD-IDENTITY",passed_items:10,total_items:10,pass_rate:1,source:"synthetic",checked_at:"2026-10-31T00:00:00Z",eval_run_ref:null,stored_eval_run_ref:null,policy:flag}});
    return true;
  });
  await release(page);
  await page.getByRole("button",{name:"只读重算评测结果",exact:true}).click();
  await arrived;
  await page.getByLabel("租户",{exact:true}).fill("other");
  await page.getByRole("button",{name:"应用开发身份",exact:true}).click();
  const received = page.waitForResponse(r => r.url().includes("/evaluation"));
  finish(); await received;
  await page.getByRole("button",{name:"刷新版本",exact:true}).click();
  assert.equal(await page.getByRole("region",{name:"评测结果只读视图",exact:true}).count(),0);
  assert.equal(await page.getByText("OLD-IDENTITY",{exact:false}).count(),0);
  assert.equal(writes().length,0);
}));
test("F11：切换版本清空评测对照并聚焦所选版本", async () => mount(engine, "agents", async ({page,writes,handle}) => {
  handle(async(route,row) => {
    if(row.path === "/agents") { await route.fulfill({json:[version,{...version,version:"2"}]}); return true; }
    if(row.path.endsWith("/evaluation")) { await route.fulfill({json:{status:"passed",reason:null,passed_items:1,total_items:1,pass_rate:1,source:"synthetic",checked_at:"2026-10-31T00:00:00Z",eval_run_ref:null,stored_eval_run_ref:null,policy:flag}});return true; }
    return false;
  });
  await page.getByRole("button",{name:"刷新版本",exact:true}).click();
  await page.getByRole("button",{name:"评测与发布",exact:true}).first().click();
  await page.getByRole("button",{name:"只读重算评测结果",exact:true}).click();
  await page.getByRole("button",{name:"保留当前结果作只读对照"}).click();
  await page.getByRole("region",{name:"评测结果并排对照"}).waitFor();
  await page.getByRole("button",{name:"评测与发布",exact:true}).nth(1).click();
  await page.waitForFunction(() => document.activeElement?.textContent?.includes("@2"));
  assert.equal(await page.getByRole("region",{name:"评测结果只读视图",exact:true}).count(),0);
  await page.getByRole("button",{name:"只读重算评测结果",exact:true}).click();
  await page.getByRole("region",{name:"评测结果只读视图",exact:true}).waitFor();
  assert.equal(await page.getByRole("region",{name:"评测结果并排对照"}).count(),0);
  assert.equal(writes().length,0);
}));
test("F11：样本明细筛选分页、未知及引用清空，不发写请求", async () => mount(engine, "agents", async ({page,writes,handle}) => {
  let unavailable = false;
  handle(async(route,row) => {
    if (!row.path.endsWith("/evaluation")) return false;
    await route.fulfill({json:{status:unavailable?"unavailable":"failed",reason:"synthetic",passed_items:unavailable?null:21,total_items:unavailable?null:22,pass_rate:unavailable?null:21/22,source:"synthetic",checked_at:"2026-10-31T00:00:00Z",eval_run_ref:null,stored_eval_run_ref:null,policy:flag,
      sample_details:unavailable?null:Array.from({length:22},(_,i)=>({sample_label:`sample-${i+1}`,verdict:i===21?"not_verifiable":"passed",reasons:i===21?["evidence_not_accepted"]:[]}))}});return true;
  });
  await release(page);
  const read=page.getByRole("button",{name:"只读重算评测结果",exact:true});
  await read.click();
  const detail=page.getByRole("region",{name:"评测样本只读明细"});
  await detail.getByText(/完整读取 22 条/).waitFor();
  assert.equal(await detail.getByRole("row").count(),21);
  await detail.getByRole("button",{name:"样本下一页"}).click();
  await detail.getByText("sample-22",{exact:true}).waitFor();
  await detail.getByLabel("样本判定筛选").selectOption("not_verifiable");
  assert.equal(await detail.getByRole("row").count(),2);
  await detail.getByText(/第 1 页/).waitFor();
  assert.match(await detail.innerText(),/汇总通过 21 \/ 22/);
  await detail.getByLabel("样本判定筛选").selectOption("not_passed");
  await detail.getByText("当前筛选无样本",{exact:true}).waitFor();
  unavailable=true;await read.click();
  await detail.getByText(/样本明细不可用/).waitFor();
  assert.equal(await detail.getByRole("row").count(),0);
  await page.getByLabel("真实评测引用",{exact:true}).fill("langfuse:changed/run");
  assert.equal(await detail.count(),0);
  assert.equal(writes().length,0);
}));


test("合成清单：预览无写入、错误清空、身份切换清空且普通用户隐藏", async () => mount(engine, "agents", async ({page,writes}) => {
 const area=page.getByRole("group",{name:"合成用例清单预览",exact:true});
 await area.getByLabel("固定用例 success",{exact:true}).check();
 await area.getByRole("button",{name:"生成预览清单",exact:true}).click();
 await area.getByRole("button",{name:"校验并预览清单",exact:true}).click();
 await page.getByRole("region",{name:"合成清单校验结果",exact:true}).waitFor();
 assert.equal(writes().length,0);
 const input=area.getByLabel("合成用例清单 JSON",{exact:true});
 const valid=await input.inputValue();
 await input.fill("{}");
 assert.equal(await page.getByRole("region",{name:"合成清单校验结果",exact:true}).count(),0);
 await area.getByRole("button",{name:"校验并预览清单",exact:true}).click();
 await area.getByRole("alert").waitFor();
 await input.fill(valid);
 await area.getByRole("button",{name:"校验并预览清单",exact:true}).click();
 await area.getByRole("combobox",{name:/^合成场景/}).selectOption("sales-summary-readonly");
 assert.equal(await input.inputValue(),"");
 assert.equal(await page.getByRole("region",{name:"合成清单校验结果",exact:true}).count(),0);
 await input.fill(valid);
 await area.getByRole("button",{name:"校验并预览清单",exact:true}).click();
 await page.reload();
 assert.equal(await input.inputValue(),"");
 assert.equal(await page.getByRole("region",{name:"合成清单校验结果",exact:true}).count(),0);
 await input.fill(valid);
 await area.getByRole("button",{name:"校验并预览清单",exact:true}).click();
 await page.getByLabel("用户",{exact:true}).fill("another-admin");
 await page.getByRole("button",{name:"应用开发身份",exact:true}).click();
 assert.equal(await input.inputValue(),"");
 assert.equal(await page.getByRole("region",{name:"合成清单校验结果",exact:true}).count(),0);
 await page.getByRole("combobox",{name:/^角色/}).selectOption("USER");
 await page.getByRole("button",{name:"应用开发身份",exact:true}).click();
 assert.equal(await area.count(),0);
 assert.equal(writes().length,0);
}));


test("F11数据集：真实隔离账本保存、重载、丢响应恢复、本人ACK及冻结", async () => mount(engine, "agents", async ({page,writes}) => {
 const tokens=JSON.parse(await readFile(profile.tokens,"utf8"));
 await page.route("**/__demo/info",route=>route.fulfill({json:{dataset_tokens:{dataset_admin:tokens.dataset_admin,dataset_unassigned:tokens.dataset_unassigned},expires_at:new Date(Date.now()+600000).toISOString()}}));
 const area=page.getByRole("region",{name:"隔离合成数据集管理",exact:true});
 const preview=page.getByRole("group",{name:"合成用例清单预览",exact:true});
 const applyDataset=async()=>{
  await area.getByLabel("数据集 Agent",{exact:true}).fill("f11-dataset-demo");
  await area.getByRole("button",{name:"加载数据集测试身份",exact:true}).click();
  await area.getByRole("status").filter({hasText:"数据集测试身份已载入内存"}).waitFor();
  await area.getByRole("button",{name:"应用数据集测试角色",exact:true}).click();
  await area.getByText("当前验签身份：default / human:dataset_admin",{exact:false}).waitFor();
  await area.getByRole("status").filter({hasText:"已核对本人账本"}).waitFor();
 };
 const restore=async()=>{
  await area.getByRole("button",{name:"只读恢复数据集账本",exact:true}).click();
  await area.getByRole("status").filter({hasText:"已核对本人账本"}).waitFor();
 };
 await preview.getByLabel("固定用例 success",{exact:true}).check();
 await preview.getByRole("button",{name:"生成预览清单",exact:true}).click();
 await preview.getByRole("button",{name:"校验并预览清单",exact:true}).click();
 await applyDataset();
 const v=`browser-${Date.now()}`;
 await area.getByLabel("新数据集版本",{exact:true}).fill(v);
 await area.getByRole("button",{name:"登记保存合成草稿",exact:true}).click();
 await area.getByRole("status").filter({hasText:"已登记"}).waitFor();
 await page.reload();await applyDataset();
 const receipt=area.getByRole("region",{name:"数据集写回执",exact:true});
 await receipt.getByText(/PREPARED/).waitFor();
 await mount(engine, "agents", async ({page:other}) => {
  const second=other.getByRole("region",{name:"隔离合成数据集管理",exact:true});
  await second.getByLabel("数据集 Agent",{exact:true}).fill("f11-dataset-demo");
  await second.getByLabel("数据集签名测试令牌",{exact:true}).fill(tokens.dataset_admin);
  await second.getByRole("button",{name:"应用数据集身份并清空",exact:true}).click();
  await second.getByRole("region",{name:"数据集写回执",exact:true}).getByText(/PREPARED/).waitFor();
  assert.equal(await second.getByRole("button",{name:"登记保存合成草稿",exact:true}).isDisabled(),true);
 });
 let executed=0;
 await page.route("**/v1/isolated-datasets/*/execute",async route=>{executed++;await route.fetch();await route.abort("failed");});
 await receipt.getByRole("button",{name:"显式执行本次数据集登记",exact:true}).click();
 await area.getByRole("status").filter({hasText:"写结果未知"}).waitFor();
 assert.equal(executed,1);
 await restore();await receipt.getByText(/COMMITTED/).waitFor();
 assert.equal(executed,1);
 await receipt.getByLabel("核对数据集终结回执后本人确认",{exact:true}).check();
 await receipt.getByRole("button",{name:"本人ACK数据集回执",exact:true}).click();
 await area.getByRole("status").filter({hasText:"本人ACK已返回"}).waitFor();
 await restore();
 await area.getByRole("button",{name:`选择 ${v}`,exact:true}).click();
 await area.getByRole("button",{name:"登记冻结所选版本",exact:true}).click();
 await area.getByRole("status").filter({hasText:"已登记"}).waitFor();
 await restore();
 await page.unroute("**/v1/isolated-datasets/*/execute");
 await receipt.getByRole("button",{name:"显式执行本次数据集登记",exact:true}).click();
 await area.getByRole("status").filter({hasText:"执行已返回"}).waitFor();
 await restore();
 await receipt.getByLabel("核对数据集终结回执后本人确认",{exact:true}).check();
 await receipt.getByRole("button",{name:"本人ACK数据集回执",exact:true}).click();
 await area.getByRole("status").filter({hasText:"本人ACK已返回"}).waitFor();
 await restore();await area.getByRole("button",{name:`选择 ${v}`,exact:true}).click();
 assert.match(await area.getByRole("region",{name:"合成数据集只读明细",exact:true}).innerText(),/frozen/);
 assert.equal(await area.getByRole("button",{name:"登记冻结所选版本",exact:true}).isDisabled(),true);
 const evaluation=page.getByRole("region",{name:"隔离合成评测执行",exact:true});
 const recoverEvaluation=async()=>{
  await evaluation.getByRole("button",{name:"只读恢复评测账本",exact:true}).click();
  await evaluation.getByRole("status").filter({hasText:"已只读核对"}).waitFor();
 };
 await recoverEvaluation();
 await evaluation.getByRole("button",{name:"登记本次合成评测",exact:true}).click();
 await evaluation.getByRole("status").filter({hasText:"请求已返回"}).waitFor();
 await page.reload();await applyDataset();await recoverEvaluation();
 const evalReceipt=evaluation.getByRole("region",{name:"合成评测回执",exact:true});
 await evalReceipt.getByText(/PREPARED/).waitFor();
 await mount(engine,"agents",async({page:other})=>{
  const registry=other.getByRole("region",{name:"隔离合成数据集管理",exact:true});
  await registry.getByLabel("数据集 Agent",{exact:true}).fill("f11-dataset-demo");
  await registry.getByLabel("数据集签名测试令牌",{exact:true}).fill(tokens.dataset_admin);
  await registry.getByRole("button",{name:"应用数据集身份并清空",exact:true}).click();
  const second=other.getByRole("region",{name:"隔离合成评测执行",exact:true});
  await second.getByRole("button",{name:"只读恢复评测账本",exact:true}).click();
  await second.getByRole("region",{name:"合成评测回执",exact:true}).getByText(/PREPARED/).waitFor();
  assert.equal(await second.getByRole("button",{name:"登记本次合成评测",exact:true}).isDisabled(),true);
 });

 let evalExecutions=0;
 await page.route("**/v1/isolated-evaluations/*/execute",async route=>{evalExecutions++;const result=await route.fetch();assert.equal(result.status(),200);await route.abort("failed");});
 await evalReceipt.getByRole("button",{name:"显式执行本次合成评测一次",exact:true}).click();
 await evaluation.getByRole("status").filter({hasText:"结果可能未知"}).waitFor();
 await recoverEvaluation();await evalReceipt.getByText(/COMMITTED/).waitFor();
 assert.equal(evalExecutions,1);
 assert.match(await evalReceipt.innerText(),/100%/);
 assert.equal(await evalReceipt.getByRole("button",{name:"显式执行本次合成评测一次",exact:true}).count(),0);
 assert.equal(await evalReceipt.getByText(/模拟工单摘要/).count(),0);
 // A late response from the original identity must not restore its receipt after switching.
 let finishRead, startedRead;
 const heldRead=new Promise(resolve=>{finishRead=resolve;});
 const arrivedRead=new Promise(resolve=>{startedRead=resolve;});
 await page.route("**/v1/isolated-evaluations/ledger",async route=>{
  const response=await route.fetch();startedRead();await heldRead;
  await route.fulfill({response}).catch(()=>{});
 });
 await evaluation.getByRole("button",{name:"只读恢复评测账本",exact:true}).click();await arrivedRead;
 await area.getByLabel("数据集签名测试令牌",{exact:true}).fill(tokens.dataset_unassigned);
 await area.getByRole("button",{name:"应用数据集身份并清空",exact:true}).click();
 await area.getByRole("status").filter({hasText:"403"}).waitFor();
 finishRead();await page.unroute("**/v1/isolated-evaluations/ledger");
 assert.equal(await page.getByRole("region",{name:"合成评测回执",exact:true}).count(),0);
 await applyDataset();await recoverEvaluation();
 await evalReceipt.getByLabel("核对评测终结回执后本人确认",{exact:true}).check();
 await evalReceipt.getByRole("button",{name:"本人ACK评测回执",exact:true}).click();
 await evaluation.getByRole("status").filter({hasText:"请求已返回"}).waitFor();
 await recoverEvaluation();assert.equal(await evalReceipt.count(),0);
 assert.equal(evalExecutions,1);
 const history=page.getByRole("region",{name:"本人评测历史与版本对照",exact:true});
 const readHistory=async()=>{
  await history.getByRole("button",{name:"读取本人评测历史",exact:true}).click();
  await history.getByRole("status").filter({hasText:"已读取本人历史"}).waitFor();
 };
 await readHistory();
 await history.getByRole("button",{name:"查看评测明细",exact:true}).first().click();
 await history.getByRole("region",{name:"历史评测明细",exact:true}).getByText(/已ACK/).waitFor();
 assert.equal(await history.getByText(/模拟工单摘要/).count(),0);
 // ACK does not erase history; a reload must read it from the server again.
 await page.reload();await applyDataset();await readHistory();
 await page.unroute("**/v1/isolated-evaluations/*/execute");
 await area.getByRole("button",{name:`选择 ${v}`,exact:true}).click();
 await recoverEvaluation();
 await evaluation.getByRole("button",{name:"登记本次合成评测",exact:true}).click();
 await evaluation.getByRole("status").filter({hasText:"请求已返回"}).waitFor();
 await recoverEvaluation();
 await evalReceipt.getByRole("button",{name:"显式执行本次合成评测一次",exact:true}).click();
 await evaluation.getByRole("status").filter({hasText:"请求已返回"}).waitFor();
 await recoverEvaluation();
 await evalReceipt.getByLabel("核对评测终结回执后本人确认",{exact:true}).check();
 await evalReceipt.getByRole("button",{name:"本人ACK评测回执",exact:true}).click();
 await evaluation.getByRole("status").filter({hasText:"请求已返回"}).waitFor();
 await readHistory();
 const checks=history.getByRole("checkbox");await checks.nth(0).check();await checks.nth(1).check();
 await history.getByRole("button",{name:"只读对照选中评测",exact:true}).click();
 await history.getByRole("region",{name:"评测版本对照结果",exact:true}).getByText("条件一致，可对照",{exact:true}).waitFor();
 assert.equal(await history.getByRole("region",{name:"历史评测明细",exact:true}).count(),2);
 const diffTable=history.getByRole("table",{name:"逐用例判定差异",exact:true});
 await diffTable.getByRole("rowheader",{name:"success",exact:true}).waitFor();
 assert.match(await diffTable.innerText(),/判定与原因相同/);
 const comparisonPanel=history.getByRole("region",{name:"评测版本对照结果",exact:true});
 assert.match(await comparisonPanel.innerText(),/左侧：Runtime 1/);
 assert.match(await comparisonPanel.innerText(),/右侧：Runtime 1/);
 await page.setViewportSize({width:375,height:812});
 const scrollArea=history.getByRole("region",{name:"逐用例差异表滚动区域",exact:true});
 await scrollArea.focus();assert.equal(await scrollArea.evaluate(el=>el===document.activeElement),true);
 await page.setViewportSize({width:1280,height:900});
 // Read-only fixture changes only returned diagnostics, not persisted results or permissions.
 await page.route("**/v1/isolated-evaluations/history/compare?**",async route=>{
  const response=await route.fetch();const value=await response.json();
  value.comparison={comparable:false,reasons:["DIFFERENT_DATASET_DIGEST"],pass_rate_delta:null};
  value.right.children=[];
  await route.fulfill({json:value});
 });
 await history.getByRole("button",{name:"只读对照选中评测",exact:true}).click();
 await comparisonPanel.getByText("不可比",{exact:true}).waitFor();
 assert.match(await comparisonPanel.innerText(),/数据集内容摘要不一致/);
 assert.match(await diffTable.innerText(),/未知（证据缺失）/);
 assert.doesNotMatch(await diffTable.innerText(),/通过 →/);
 await page.unroute("**/v1/isolated-evaluations/history/compare?**");
 await page.route("**/v1/isolated-evaluations/history?**",async route=>{
  const url=new URL(route.request().url());const response=await route.fetch();const value=await response.json();
  if (!url.searchParams.has("before")) {value.items=value.items.slice(0,1);value.next_cursor=value.items[0].attempt_id;}
  await route.fulfill({json:value});
 });
 await readHistory();
 await history.getByRole("button",{name:"下一页评测历史",exact:true}).click();
 await history.getByText(/后续页 · 本页/).waitFor();
 assert.equal(await history.getByRole("checkbox",{checked:true}).count(),0);
 await history.getByRole("button",{name:"返回评测历史首页",exact:true}).click();
 await history.getByText(/首页 · 本页/).waitFor();
 assert.equal(await history.getByRole("button",{name:"返回评测历史首页",exact:true}).isDisabled(),true);
 await page.unroute("**/v1/isolated-evaluations/history?**");


 // Errors discard previously displayed details rather than retaining stale successful results.
 await page.route("**/v1/isolated-evaluations/history?**",route=>route.fulfill({status:403,contentType:"application/json",body:'{"detail":"revoked"}'}));
 await history.getByRole("button",{name:"读取本人评测历史",exact:true}).click();
 await history.getByRole("status").filter({hasText:"403"}).waitFor();
 assert.equal(await history.getByRole("region",{name:"历史评测明细",exact:true}).count(),0);
 await page.unroute("**/v1/isolated-evaluations/history?**");
 let releaseHistory, historyArrived;
 const waitingHistory=new Promise(resolve=>{releaseHistory=resolve;});
 const arrivedHistory=new Promise(resolve=>{historyArrived=resolve;});
 await page.route("**/v1/isolated-evaluations/history?**",async route=>{
  const response=await route.fetch();historyArrived();await waitingHistory;
  await route.fulfill({response}).catch(()=>{});
 });
 await history.getByRole("button",{name:"读取本人评测历史",exact:true}).click();await arrivedHistory;
 await area.getByLabel("数据集签名测试令牌",{exact:true}).fill(tokens.dataset_unassigned);
 await area.getByRole("button",{name:"应用数据集身份并清空",exact:true}).click();
 await area.getByRole("status").filter({hasText:"403"}).waitFor();
 releaseHistory();await page.unroute("**/v1/isolated-evaluations/history?**");
 assert.equal(await history.getByRole("listitem").count(),0);
 assert.equal(await history.getByRole("region",{name:"历史评测明细",exact:true}).count(),0);

 await area.getByLabel("数据集签名测试令牌",{exact:true}).fill(tokens.dataset_unassigned);
 await area.getByRole("button",{name:"应用数据集身份并清空",exact:true}).click();
 await area.getByRole("status").filter({hasText:"403"}).waitFor();
 assert.equal(await area.getByRole("region",{name:"合成数据集只读明细",exact:true}).count(),0);
 assert.equal(writes().length,0);
}));
