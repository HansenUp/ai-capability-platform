import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";
const source = readFileSync(new URL("../../../packages/sdk-web/src/run-client.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
const { RuntimeRunClient } = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`);
const row = (seq, text = "合成回答") => ({ run_id: "run1", seq, type: "model.response", payload: { text } });
const frame = value => `event: ${value.type}\r\nid: ${value.seq}\r\ndata: ${JSON.stringify(value)}\r\n\r\n`;
const done = 'event: done\ndata: {"type":"done","run_id":"run1","stop_reason":"awaiting"}\n\n';
async function collect(stream) { const result = []; for await (const event of stream) result.push(event); return result; }
function clientWith(text, calls = [], split = false) {
  return new RuntimeRunClient(async () => ({ Authorization: "Bearer synthetic" }), async (...args) => {
    calls.push(args);
    const encoded = new TextEncoder().encode(text);
    return new Response(new ReadableStream({ start(controller) {
      if (split) for (const byte of encoded) controller.enqueue(Uint8Array.of(byte));
      else controller.enqueue(encoded);
      controller.close();
    } }), { headers: { "Content-Type": "text/event-stream; charset=utf-8" } });
  });
}
test("SSE：UTF-8逐字节、CRLF分片、注释与多行data，空闲ping不推进游标", async () => {
  const calls = [], value = row(3);
  const text = ': ping\r\n\r\nretry: 1\n\n' + frame(value).replace('data: {', 'data: {\ndata: ') + done;
  assert.deepEqual(await collect(clientWith(text, calls, true).follow("run1", 1)), [value]);
  assert.equal(calls.length, 1); assert.equal(calls[0][0], "/v1/runs/run1/stream?since_seq=1");
  assert.equal(calls[0][1].method, "GET"); assert.equal(calls[0][1].headers.Authorization, "Bearer synthetic");
  assert.equal(calls[0][1].redirect, "error"); assert.equal(calls[0][1].body, undefined);
});
test("SSE：断连不自动重试，手动从最后已确认序号续读，合法缺号保留", async () => {
  const seen = [], calls = [];
  await assert.rejects(async () => { for await (const event of clientWith(frame(row(2)), calls).follow("run1")) seen.push(event); });
  assert.deepEqual(seen, [row(2)]); assert.equal(calls.length, 1);
  assert.deepEqual(await collect(clientWith(frame(row(5)) + done, calls).follow("run1", 2)), [row(5)]);
  assert.equal(calls[1][0], "/v1/runs/run1/stream?since_seq=2");
});
test("SSE：跨Run、错误id/type、重复／倒序／非法序号、坏payload与伪done失败关闭", async () => {
  for (const text of [frame({ ...row(1), run_id: "foreign" }), frame(row(1)).replace('id: 1', 'id: 2'),
    frame(row(1)).replace('event: model.response', 'event: other'), frame(row(1)) + frame(row(1)),
    frame(row(2)) + frame(row(1)), frame(row(0)), frame({ ...row(1), seq: 2 ** 53 }),
    frame({ ...row(1), payload: null }), frame({ ...row(1), payload: [] }),
    done.replace('run1', 'foreign'), done.replace('awaiting', 'unknown'), 'event: model.response\ndata: invalid\n\n']) {
    await assert.rejects(collect(clientWith(text).follow("run1")), error => !error.message.includes("foreign"));
  }
});
test("SSE：401／403／404／5xx不读取敏感正文、不重试且不是未知写", async () => {
  for (const status of [401, 403, 404, 503]) {
    let calls = 0;
    const client = new RuntimeRunClient(async () => ({}), async () => { calls++; return new Response("sensitive", { status }); });
    await assert.rejects(collect(client.follow("run1")), error => error.status === status && !error.outcomeUnknown && !error.message.includes("sensitive"));
    assert.equal(calls, 1);
  }
});
test("SSE：拒绝非SSE、半帧EOF、超过单帧上限，不展示残缺事件", async () => {
  const wrong = new RuntimeRunClient(async () => ({}), async () => Response.json(row(1)));
  await assert.rejects(collect(wrong.follow("run1")));
  await assert.rejects(collect(clientWith(frame(row(1)).slice(0, -4)).follow("run1")));
  await assert.rejects(collect(clientWith('data: ' + 'x'.repeat(1048577)).follow("run1")));
});
test("SSE：等待身份时取消不发请求，非法游标不发请求", async () => {
  let release, count = 0;
  const barrier = new Promise(resolve => { release = resolve; }), controller = new AbortController();
  const client = new RuntimeRunClient(async () => { await barrier; return {}; }, async () => { count++; return Response.json({}); });
  const pending = collect(client.follow("run1", 0, controller.signal));
  controller.abort(); release(); await assert.rejects(pending, error => error.name === "AbortError");
  for (const cursor of [-1, NaN, 2 ** 53]) await assert.rejects(collect(client.follow("run1", cursor)));
  assert.equal(count, 0);
});
test("SSE：传输忽略取消且响应迟到，释放body、不泄漏旧身份事件", async () => {
  let release, entered, canceled = 0;
  const barrier = new Promise(resolve => { release = resolve; }), ready = new Promise(resolve => { entered = resolve; });
  const controller = new AbortController();
  const client = new RuntimeRunClient(async () => ({}), async () => {
    entered(); await barrier;
    return new Response(new ReadableStream({ cancel() { canceled++; } }), { headers: { "content-type": "text/event-stream" } });
  });
  const pending = collect(client.follow("run1", 0, controller.signal));
  await ready; controller.abort(); release(); await assert.rejects(pending, error => error.name === "AbortError");
  assert.equal(canceled, 1);
});
test("SSE：取消挂起read或消费者提前结束，均释放reader且不继续请求", async () => {
  let entered, canceled = 0;
  const ready = new Promise(resolve => { entered = resolve; }), controller = new AbortController();
  const client = new RuntimeRunClient(async () => ({}), async () => new Response(new ReadableStream({
    pull() { entered(); }, cancel() { canceled++; },
  }), { headers: { "content-type": "text/event-stream" } }));
  const pending = collect(client.follow("run1", 0, controller.signal));
  await ready; controller.abort(); await assert.rejects(pending, error => error.name === "AbortError");
  assert.equal(canceled, 1);
  let earlyCanceled = 0;
  const early = new RuntimeRunClient(async () => ({}), async () => new Response(new ReadableStream({
    start(controller) { controller.enqueue(new TextEncoder().encode(frame(row(1)))); },
    cancel() { earlyCanceled++; },
  }), { headers: { "content-type": "text/event-stream" } }));
  for await (const event of early.follow("run1")) { assert.equal(event.seq, 1); break; }
  assert.equal(earlyCanceled, 1);
});
