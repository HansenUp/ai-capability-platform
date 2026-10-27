import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";

const source = readFileSync(new URL("../src/node-commands.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
const commands = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`);
const operator = { user: "ops", role: "TENANT_ADMIN", tenant: "a" };
const nodes = [{ node_id: "simulated" }];
const form = { node: "simulated", type: "reload_config", pollInterval: "5", timeout: "2", restartTimeout: "30",
  agent: "agent", base: "1", expected: "1", target: "2", alias: "chat", skills: [{ name: "demo", version: "1", sha: "a".repeat(64) }] };

test("固定四类与注册节点、角色边界", () => {
  for (const kind of ["reload_config", "reload_skills", "switch_model", "graceful_restart"]) {
    assert.equal(commands.prepareCommand({ ...form, type: kind }, { ...operator, role: "PLATFORM_ADMIN" }, nodes).body.type, kind);
  }
  for (const role of ["USER", "ADMIN"]) assert.throws(() => commands.prepareCommand(form, { ...operator, role }, nodes));
  assert.throws(() => commands.prepareCommand({ ...form, type: "shell" }, operator, nodes));
  assert.throws(() => commands.prepareCommand({ ...form, node: "real-node" }, operator, nodes));
  assert.throws(() => commands.prepareCommand({ ...form, type: "graceful_restart" }, operator, nodes));
});
test("reload_config 仅允许有界代理参数且忽略扩展的安全配置输入", () => {
  assert.deepEqual(commands.prepareCommand({ ...form, pollInterval: 5, timeout: 2 }, operator, nodes).body.args,
    { poll_interval_s: 5, timeout_s: 2 });
  assert.deepEqual(commands.prepareCommand({ ...form, console_url: "http://evil", args: { auth_required: false } }, operator, nodes).body,
    { type: "reload_config", args: { poll_interval_s: 5, timeout_s: 2 } });
  for (const value of ["0", "3601", "NaN", "Infinity"]) assert.throws(() => commands.prepareCommand({ ...form, pollInterval: value }, operator, nodes));
  assert.throws(() => commands.prepareCommand({ ...form, pollInterval: "", timeout: "" }, operator, nodes));
});
test("模型 CAS 必填、租户与授权角色只能来自操作者头，不进入 args", () => {
  const prepared = commands.prepareCommand({ ...form, type: "switch_model", tenant_id: "evil", authorization_role: "PLATFORM_ADMIN" }, operator, nodes);
  assert.deepEqual(prepared.body.args, { agent_id: "agent", base_version: "1", expected_version: "1", target_version: "2", model_alias: "chat" });
  assert.equal(prepared.operator.tenant, "a");
  assert.throws(() => commands.prepareCommand({ ...form, type: "switch_model", expected: "" }, operator, nodes));
});
test("重启仅 timeout_s 且限制 0.1～300，不允许 PID／shell 参数", () => {
  const op = { ...operator, role: "PLATFORM_ADMIN" };
  const prepared = commands.prepareCommand({ ...form, type: "graceful_restart", pid: 1, shell: "kill" }, op, nodes);
  assert.deepEqual(prepared.body.args, { timeout_s: 30 });
  for (const value of ["0", "301", "NaN"]) assert.throws(() => commands.prepareCommand({ ...form, type: "graceful_restart", restartTimeout: value }, op, nodes));
});
test("Skill 引用需要哈希且拒绝路径、空列表与重复声明", () => {
  for (const skills of [[], [{ ...form.skills[0], name: "../demo" }], [{ ...form.skills[0], sha: "bad" }], [...form.skills, ...form.skills]]) {
    assert.throws(() => commands.prepareCommand({ ...form, type: "reload_skills", skills }, operator, nodes));
  }
});
test("预览是独立快照，不随表单或身份对象修改", () => {
  const input = structuredClone(form), op = { ...operator };
  const prepared = commands.prepareCommand({ ...input, type: "reload_skills" }, op, nodes);
  input.skills[0].name = "changed"; op.tenant = "changed";
  assert.equal(prepared.operator.tenant, "a");
  assert.equal(prepared.body.args.skills[0].name, "demo");
});
test("一次 POST 接收 202 ID，明确禁止自动重试或跟随重定向", async () => {
  const calls = [];
  const prepared = commands.prepareCommand(form, operator, nodes);
  const id = await commands.submitCommand(prepared, true, async (...args) => { calls.push(args); return Response.json({ id: "one" }, { status: 202 }); });
  assert.equal(id, "one"); assert.equal(calls.length, 1);
  assert.equal(calls[0][1].method, "POST"); assert.equal(calls[0][1].redirect, "error");
  assert.equal(calls[0][1].headers.Authorization, undefined);
  assert.deepEqual(JSON.parse(calls[0][1].body), prepared.body);
});
test("网络丢失、5xx、错误 202 内容均结果未知且不重发", async () => {
  for (const responder of [() => { throw new Error("network"); }, () => new Response("secret", { status: 503 }),
    () => Response.json({}, { status: 202 }), () => new Response("bad", { status: 202 })]) {
    let count = 0;
    await assert.rejects(commands.submitCommand(commands.prepareCommand(form, operator, nodes), true, async () => { count++; return responder(); }),
      error => error.outcomeUnknown === true && !error.message.includes("secret"));
    assert.equal(count, 1);
  }
});
test("明确权限拒绝与生产构建失败关闭，不假装已入队", async () => {
  const prepared = commands.prepareCommand(form, operator, nodes);
  await assert.rejects(commands.submitCommand(prepared, true, async () => new Response("secret", { status: 403 })),
    error => error.outcomeUnknown === false && !error.message.includes("secret"));
  let called = false;
  await assert.rejects(commands.submitCommand(prepared, false, async () => { called = true; return Response.json({}); }));
  assert.equal(called, false);
});
