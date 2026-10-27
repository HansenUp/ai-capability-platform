import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";
const compiled = ts.transpileModule(readFileSync(new URL("../../../packages/sdk-web/src/run-client.ts", import.meta.url), "utf8"),
  { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
const { RuntimeRunClient } = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`);
const scope = "a".repeat(64), id = "11111111-1111-4111-8111-111111111111";
const identity = { scope_id: scope, protocol: "workbench-v1", auth_method: "user", tenant_id: "tenant-a", subject: "human:alice" };
const prepared = { scope_id: scope, attempt_id: id, run_id: "run1", action: "credential", request_seq: 4,
  fingerprint: "b".repeat(64), status: "PREPARED", revision: 1, acknowledged: false, evidence_seq: null, rejection: null };
const committed = { ...prepared, status: "COMMITTED", revision: 2, evidence_seq: 5 };
test("创建账本 SDK：无RunID登记、预留范围及原消息显式执行，ACK不带消息", async () => {
  const row = { ...prepared, action: "create", run_id: "run_" + "a".repeat(20), request_seq: 0, value_hmac: "d".repeat(64),
    create_agent: "demo", create_version: "1", create_model: "default-chat", create_snapshot: "e".repeat(64), create_reason: "only" };
  const calls = [], client = new RuntimeRunClient(async () => ({}), async (path, options) => {
    calls.push([path, JSON.parse(options.body)]);
    return Response.json(path.endsWith("execute") ? { ...row, status: "COMMITTED", revision: 2, evidence_seq: 1 }
      : path.endsWith("acknowledge") ? { ...row, status: "COMMITTED", revision: 3, evidence_seq: 1, acknowledged: true } : row);
  });
  assert.deepEqual(await client.prepareCreate(" demo ", "original", scope, id), row);
  await assert.rejects(client.settleAttempt(row)); assert.equal(calls.length, 1);
  const receipt = await client.settleAttempt(row, false, "original");
  await client.settleAttempt(receipt, true);
  assert.deepEqual(calls.map(row => row[1]), [{ agent_id: "demo", text: "original", attempt_id: id },
    { fingerprint: row.fingerprint, revision: 1, value: "original" }, { fingerprint: row.fingerprint, revision: 2 }]);
});
test("创建账本 SDK：预留序号、Agent快照和终结事件必须严格匹配", async () => {
  const row = { ...prepared, action: "create", request_seq: 0, value_hmac: "d".repeat(64), create_agent: "demo",
    create_version: "1", create_model: "default-chat", create_snapshot: "e".repeat(64), create_reason: "only" };
  for (const bad of [{ ...row, create_agent: "other" }, { ...row, create_snapshot: "" }, { ...row, request_seq: 1 }]) {
    const client = new RuntimeRunClient(async () => ({}), async () => Response.json(bad));
    await assert.rejects(client.prepareCreate("demo", "original", scope, id), error => error.outcomeUnknown);
  }
  for (const changed of [{ create_version: "2" }, { create_model: "other" }, { create_snapshot: "f".repeat(64) }, { evidence_seq: 2 }]) {
    const client = new RuntimeRunClient(async () => ({}), async () => Response.json({ ...row, status: "COMMITTED", revision: 2, evidence_seq: 1, ...changed }));
    await assert.rejects(client.settleAttempt(row, false, "original"), error => error.outcomeUnknown);
  }
});
test("回答账本 SDK：原值只在登记与显式执行正文，null有效，收口不携带原值", async () => {
  for (const value of [null, { id: "synthetic" }]) {
    const row = { ...prepared, action: "answer", call_id: "ask", value_hmac: "d".repeat(64) }, calls = [];
    const client = new RuntimeRunClient(async () => ({}), async (path, options) => {
      calls.push([path, JSON.parse(options.body)]);
      return Response.json(path.endsWith("execute") ? { ...row, status: "COMMITTED", revision: 2, evidence_seq: 5 }
        : path.endsWith("acknowledge") ? { ...row, status: "COMMITTED", revision: 3, evidence_seq: 5, acknowledged: true } : row);
    });
    assert.deepEqual(await client.prepareAnswer("run1", { kind: "answer", call_id: "ask" }, value, scope, id), row);
    await assert.rejects(client.settleAttempt(row)); assert.equal(calls.length, 1);
    const result = await client.settleAttempt(row, false, value);
    await client.settleAttempt(result, true);
    assert.deepEqual(calls.map(row => row[1]), [{ run_id: "run1", attempt_id: id, action: "answer", call_id: "ask", value },
      { fingerprint: row.fingerprint, revision: 1, value }, { fingerprint: row.fingerprint, revision: 2 }]);
  }
});
test("回答账本 SDK：错误或变更的内容绑定、混淆审批字段均拒绝接受", async () => {
  const row = { ...prepared, action: "answer", call_id: "ask", value_hmac: "d".repeat(64) };
  for (const invalid of [{ ...row, value_hmac: undefined }, { ...row, params_hash: "not-answer" }, { ...row, call_id: "other" }]) {
    const client = new RuntimeRunClient(async () => ({}), async () => Response.json(invalid));
    await assert.rejects(client.prepareAnswer("run1", { kind: "answer", call_id: "ask" }, null, scope, id), error => error.outcomeUnknown);
  }
  const client = new RuntimeRunClient(async () => ({}), async () => Response.json({ ...row, value_hmac: "e".repeat(64), status: "COMMITTED", revision: 2, evidence_seq: 5 }));
  await assert.rejects(client.settleAttempt(row, false, null), error => error.outcomeUnknown);
});
test("账本 SDK：验签身份和主体账本必须匹配；GET 无写效果且 no-store", async () => {
  const calls = [];
  const client = new RuntimeRunClient(async () => ({ Authorization: "Bearer synthetic" }), async (path, options) => {
    calls.push([path, options]); return Response.json(path.endsWith("identity") ? identity : { scope_id: scope, active: prepared });
  });
  assert.deepEqual(await client.ledger(), { identity, active: prepared });
  assert.deepEqual(calls.map(([path, options]) => [path, options.method, options.cache]),
    [["/v1/workbench/identity", "GET", "no-store"], ["/v1/workbench/attempts", "GET", "no-store"]]);
  for (const active of [undefined, { ...prepared, scope_id: "c".repeat(64) }, { ...prepared, acknowledged: true }, { ...committed, evidence_seq: 3 }]) {
    const invalid = new RuntimeRunClient(async () => ({}), async path => Response.json(path.endsWith("identity") ? identity : { scope_id: scope, active }));
    await assert.rejects(invalid.ledger());
  }
});
test("账本 SDK：prepare、execute、acknowledge 精确匹配，凭据不进入正文", async () => {
  const calls = [];
  const client = new RuntimeRunClient(async () => ({ Authorization: "Bearer synthetic" }), async (path, options) => {
    calls.push([path, JSON.parse(options.body)]);
    return Response.json(path.endsWith("execute") ? committed : path.endsWith("acknowledge") ? { ...committed, revision: 3, acknowledged: true } : prepared);
  });
  assert.deepEqual(await client.prepareCredential("run1", scope, id), prepared);
  assert.deepEqual(await client.settleAttempt(prepared), committed);
  assert.equal((await client.settleAttempt(committed, true)).acknowledged, true);
  assert.deepEqual(calls.map(row => row[1]), [{ run_id: "run1", attempt_id: id },
    { fingerprint: prepared.fingerprint, revision: 1 }, { fingerprint: prepared.fingerprint, revision: 2 }]);
  assert.equal(JSON.stringify(calls).includes("synthetic"), false);
});
test("账本 SDK：拒绝错误终结回执，不将坏响应当作收口或自动重试", async () => {
  for (const row of [{ ...committed, run_id: "other" }, { ...committed, attempt_id: "22222222-2222-4222-8222-222222222222" },
    { ...committed, fingerprint: "c".repeat(64) }, { ...committed, revision: 1 }, { ...committed, acknowledged: true }, prepared]) {
    let writes = 0;
    const client = new RuntimeRunClient(async () => ({}), async () => { writes++; return Response.json(row); });
    await assert.rejects(client.settleAttempt(prepared), error => error.outcomeUnknown === true);
    assert.equal(writes, 1);
  }
  const client = new RuntimeRunClient(async () => ({}), async () => { throw new Error("transport lost"); });
  await assert.rejects(client.settleAttempt(prepared), error => error.outcomeUnknown === true);
});
test("账本 SDK：读取取消后即使旧 JSON 返回也不能接受身份或继续查询", async () => {
  const controller = new AbortController(); let release, calls = 0;
  const client = new RuntimeRunClient(async () => ({}), async () => {
    calls++; return { ok: true, status: 200, json: () => new Promise(resolve => { release = resolve; }) };
  });
  const pending = client.ledger(controller.signal);
  while (!release) await new Promise(resolve => setImmediate(resolve));
  controller.abort(); release(identity);
  await assert.rejects(pending, { name: "AbortError" }); assert.equal(calls, 1);
});
test("审批账本 SDK：批准与拒绝分别登记调用指纹，不携带TTL、意见或伪造身份", async () => {
  for (const approved of [true, false]) {
    const action = approved ? "approve" : "reject", calls = [];
    const row = { ...prepared, action, call_id: "call1", params_hash: "params" };
    const client = new RuntimeRunClient(async () => ({ Authorization: "Bearer reviewer" }), async (path, options) => {
      calls.push([path, JSON.parse(options.body)]);
      return Response.json(path.endsWith("/execute") ? { ...row, status: "COMMITTED", revision: 2, evidence_seq: 5 } : row);
    });
    assert.deepEqual(await client.prepareApproval("run1", { call_id: "call1", params_hash: "params" }, approved, scope, id), row);
    assert.equal((await client.settleAttempt(row)).action, action);
    assert.deepEqual(calls, [["/v1/workbench/attempts", { run_id: "run1", attempt_id: id, action, call_id: "call1", params_hash: "params" }],
      [`/v1/workbench/attempts/${id}/execute`, { fingerprint: row.fingerprint, revision: 1 }]]);
  }
});
test("审批账本 SDK：同attempt换动作或调用指纹的登记／执行回执保持未知", async () => {
  const row = { ...prepared, action: "approve", call_id: "call1", params_hash: "params" };
  for (const value of [prepared, { ...row, action: "reject" }, { ...row, call_id: "other" }, { ...row, params_hash: "different" }]) {
    const client = new RuntimeRunClient(async () => ({}), async () => Response.json(value));
    await assert.rejects(client.prepareApproval("run1", { call_id: "call1", params_hash: "params" }, true, scope, id), error => error.outcomeUnknown === true);
    const settled = { ...value, status: "COMMITTED", revision: 2, evidence_seq: 5 };
    const executor = new RuntimeRunClient(async () => ({}), async () => Response.json(settled));
    await assert.rejects(executor.settleAttempt(row), error => error.outcomeUnknown === true);
  }
  const wrong = new RuntimeRunClient(async () => ({}), async () => Response.json(row));
  await assert.rejects(wrong.prepareCredential("run1", scope, id), error => error.outcomeUnknown === true);
});
