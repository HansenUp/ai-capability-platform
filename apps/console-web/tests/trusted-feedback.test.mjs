import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";
const compiled = ts.transpileModule(readFileSync(new URL("../../../packages/sdk-web/src/run-client.ts", import.meta.url), "utf8"),
  { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
const { RuntimeRunClient } = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`);
const scope = "a".repeat(64), id = "11111111-1111-4111-8111-111111111111";
const identity = { scope_id: scope, protocol: "workbench-v1", auth_method: "user", tenant_id: "a", subject: "human:alice" };
const prepared = { scope_id: scope, attempt_id: id, action: "feedback", run_id: "run1", request_seq: 4, fingerprint: "b".repeat(64),
  payload_hash: "b".repeat(64), score: 1, score_revision: 1, status: "PREPARED", revision: 1, acknowledged: false, evidence_seq: null, rejection: null };
const pending = { ...prepared, status: "PENDING_OBSERVATION", revision: 2 };
const observed = { ...prepared, status: "OBSERVED", revision: 3 };
test("可信反馈 SDK：登记／单次发送／只读对账／本人ACK严格分离", async () => {
  const calls = [], client = new RuntimeRunClient(async () => ({ Authorization: "Bearer isolated" }), async (path, options) => {
    calls.push([path, options.method, options.body && JSON.parse(options.body)]);
    if (path.endsWith("/head")) return Response.json({ scope_id: scope, run_id: "run1", score_revision: 0 });
    return Response.json(path.endsWith("execute") ? pending : path.endsWith("reconcile") ? observed
      : path.endsWith("acknowledge") ? { ...observed, revision: 4, acknowledged: true } : prepared);
  });
  assert.equal(await client.feedbackHead("run1", scope), 0);
  const row = await client.prepareFeedback("run1", 1, 0, scope, id);
  await assert.rejects(client.feedbackAction(row, "acknowledge"));
  const sent = await client.settleAttempt(row);
  await assert.rejects(client.settleAttempt(sent));
  const proof = await client.feedbackAction(sent, "reconcile");
  await client.settleAttempt(proof, true);
  assert.deepEqual(calls.map(call => call[1]), ["GET", "POST", "POST", "POST", "POST"]);
  assert.equal(calls.filter(call => call[0].endsWith("execute")).length, 1);
  assert.deepEqual(calls[1][2], { attempt_id: id, run_id: "run1", score: 1, expected_revision: 0 });
});
test("可信反馈 SDK：主体锁明细补读只用GET，缺失或伪造证据拒收", async () => {
  const calls = [], client = new RuntimeRunClient(async () => ({}), async (path, options) => {
    calls.push([path, options.method]);
    return Response.json(path.endsWith("identity") ? identity : path.endsWith("attempts") ? { scope_id: scope, active: prepared } : pending);
  });
  assert.equal((await client.ledger()).active.status, "PENDING_OBSERVATION");
  assert.equal(calls.length, 3); assert.ok(calls.every(call => call[1] === "GET"));
  for (const change of [{ score: true }, { score_revision: 0 }, { payload_hash: "c".repeat(64) }, { status: "COMMITTED" }, { revision: 9 }, { scope_id: "c".repeat(64) }]) {
    const broken = new RuntimeRunClient(async () => ({}), async () => Response.json({ ...prepared, ...change }));
    await assert.rejects(broken.feedbackReceipt(id, scope));
  }
});
test("可信反馈 SDK：旧响应abort后不继续明细查询，异Run／修订不可覆盖", async () => {
  const controller = new AbortController(), calls = [];
  const client = new RuntimeRunClient(async () => ({}), async path => {
    calls.push(path);
    if (path.endsWith("identity")) return Response.json(identity);
    return { ok: true, status: 200, json: async () => { controller.abort(); return { scope_id: scope, active: prepared }; } };
  });
  await assert.rejects(client.ledger(controller.signal), error => error.name === "AbortError");
  assert.equal(calls.length, 2);
  for (const change of [{ run_id: "foreign" }, { score_revision: 2 }, { score: 0 }, { status: "OBSERVED", revision: 3 }]) {
    const broken = new RuntimeRunClient(async () => ({}), async () => Response.json({ ...pending, ...change }));
    await assert.rejects(broken.feedbackAction(prepared, "execute"), error => error.outcomeUnknown);
  }
});
test("可信反馈 SDK：网络未知与404证据不触发重投", async () => {
  let count = 0;
  const disconnected = new RuntimeRunClient(async () => ({}), async () => { count++; throw new Error("disconnect"); });
  await assert.rejects(disconnected.feedbackAction(prepared, "execute"), error => error.outcomeUnknown);
  assert.equal(count, 1);
  const unresolved = new RuntimeRunClient(async () => ({}), async () => Response.json(pending));
  assert.deepEqual(await unresolved.feedbackAction(pending, "reconcile"), pending);
  await assert.rejects(unresolved.feedbackAction(pending, "acknowledge"));
});
