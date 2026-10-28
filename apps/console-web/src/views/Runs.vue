<script setup lang="ts">
import { computed, inject, onBeforeUnmount, reactive, ref } from "vue";
import { useRoute } from "vue-router";
import RunOutcome from "../components/RunOutcome.vue";
import { eventStage, runHint } from "../run-outcome";
import { demoContextKey } from "../demo-context";
import { doneTrace, mergeEvents, traceLink, type RunEvent } from "../run-debug";
interface Run { run_id: string; agent_id: string; agent_version: string; subject: string; state: string; last_seq: number; created_at: string }
const route = useRoute();
const linkedRun = computed(() => runHint(route?.query.run));
const mode = ref("token");
const token = ref("");
const subject = ref("human:ops");
const tenant = ref("default");
const agent = ref("");
const runId = ref("");
const rows = ref<Run[]>([]);
const cursor = ref<string | null>(null);
const selected = ref<Run | null>(null);
const events = ref<RunEvent[]>([]);
const trace = ref("");
const message = ref("");
const busy = ref(false);
const applied = ref(false);
const credentials = reactive<Record<string, string>>({});
const development = import.meta.env.DEV;
const demo = inject(demoContextKey, null);
function applyDemo() {
  if (!demo || busy.value) return;
  mode.value = "token"; token.value = demo.token(); apply();
  if (applied.value) { runId.value = demo.run.value; agent.value = demo.agent.value; }
}
const traceUrl = computed(() => traceLink(import.meta.env.VITE_LANGFUSE_HOST ?? "http://localhost:3000", import.meta.env.VITE_LANGFUSE_PROJECT_ID ?? "", trace.value));
const toolNames = computed(() => new Map(events.value.filter(event => event.type === "tool.requested").map(event => [event.payload.call_id, event.payload.tool])));
let controller: AbortController | null = null;
let loadedAgent = "";
let generation = 0;
function assertCurrent(current: number) {
  if (current !== generation) throw new DOMException("读取上下文已改变", "AbortError");
}
function clear() { rows.value = []; cursor.value = null; selected.value = null; events.value = []; trace.value = ""; }
function apply() {
  if (busy.value) return;
  generation += 1;
  clear(); runId.value = "";
  for (const key of Object.keys(credentials)) delete credentials[key];
  applied.value = false;
  if (mode.value === "token" && token.value.trim()) credentials.Authorization = `Bearer ${token.value.trim()}`;
  else if (mode.value === "dev" && development && subject.value.trim() && tenant.value.trim()) {
    credentials["X-AI-Subject"] = subject.value.trim(); credentials["X-AI-Tenant"] = tenant.value.trim();
  } else { message.value = "请提供用户访问令牌或本机开发身份"; return; }
  token.value = "";
  applied.value = true;
  message.value = "身份已应用，旧轨迹已清空。令牌只在当前页面内存中，离开页面即释放。";
}
async function request(path: string, current: number): Promise<Response> {
  assertCurrent(current);
  const response = await fetch(path, { headers: { ...credentials }, signal: controller?.signal, redirect: "error", cache: "no-store" });
  assertCurrent(current);
  if (!response.ok) {
    if ([401, 403, 404].includes(response.status)) clear();
    throw new Error(`Runtime HTTP ${response.status}；未取得读取授权或资源不存在时不会保留旧轨迹`);
  }
  return response;
}
async function perform(action: (current: number) => Promise<void>) {
  if (busy.value || !applied.value) return;
  busy.value = true; message.value = "";
  const current = generation;
  const activeController = new AbortController();
  controller = activeController;
  const timeout = setTimeout(() => activeController.abort(), 15000);
  try { await action(current); } catch (error) { if (current === generation) message.value = String(error); }
  finally { clearTimeout(timeout); if (current === generation) { controller = null; busy.value = false; } }
}
async function list(more = false) {
  await perform(async current => {
    if (more && agent.value.trim() !== loadedAgent) throw new Error("筛选已变化，请先刷新列表");
    const query = new URLSearchParams({ limit: "20" });
    loadedAgent = agent.value.trim();
    if (loadedAgent) query.set("agent_id", loadedAgent);
    if (more && cursor.value) query.set("cursor", cursor.value);
    const result: { items: Run[]; next_cursor: string | null } = await (await request(`/v1/runs?${query}`, current)).json();
    assertCurrent(current);
    rows.value = more ? [...rows.value, ...result.items] : result.items;
    cursor.value = result.next_cursor;
  });
}
async function inspect(id: string, incremental = false) {
  await perform(async current => {
    if (!id.trim()) throw new Error("请输入 Run ID");
    const same = incremental && selected.value?.run_id === id;
    if (!same) { selected.value = null; events.value = []; trace.value = ""; }
    const path = `/v1/runs/${encodeURIComponent(id)}`;
    const run: Run = await (await request(path, current)).json();
    assertCurrent(current);
    if (run.run_id !== id) throw new Error("Run 响应范围不匹配");
    const since = same ? (events.value.at(-1)?.seq ?? 0) : 0;
    const incoming: RunEvent[] = await (await request(`${path}/events?since_seq=${since}`, current)).json();
    assertCurrent(current);
    events.value = mergeEvents(same ? events.value : [], incoming, id);
    selected.value = run;
    trace.value = "";
    if (["completed", "failed", "canceled"].includes(run.state)) {
      const response = await request(`${path}/stream?since_seq=${run.last_seq}`, current);
      const reader = response.body?.getReader();
      if (reader) {
        const decoder = new TextDecoder(); let buffer = "";
        try {
          while (true) {
            const chunk = await reader.read(); assertCurrent(current); if (chunk.done) break;
            buffer += decoder.decode(chunk.value, { stream: true });
            if (buffer.length > 65536) throw new Error("Trace 尾帧超过读取上限");
            const frames = buffer.replace(/\r\n/g, "\n").split("\n\n"); buffer = frames.pop() ?? "";
            for (const frame of frames) { const value = doneTrace(frame); if (value) trace.value = value; }
            if (trace.value) break;
          }
        } finally { await reader.cancel(); }
      }
    }
  });
}
onBeforeUnmount(() => { generation += 1; controller?.abort(); for (const key of Object.keys(credentials)) delete credentials[key]; token.value = ""; clear(); });
</script>
<template>
  <h2>Run 只读调试轨迹</h2>
  <button v-if="demo" :disabled="busy || !demo.role.value" @click="applyDemo">应用演示身份并定位同Run</button>
  <p>直接读取 Runtime，沿用发起人／审批人／管理员权限和租户边界；不使用 Console 服务账号，不审批、不续跑、不取消 Run。开发态 Runtime 不检查管理员角色，不能代替生产鉴权验收。</p>
  <fieldset :disabled="busy"><legend>Runtime 读取身份（不持久化）</legend>
    <label>鉴权方式 <select v-model="mode"><option value="token">用户 Bearer 令牌</option><option v-if="development" value="dev">本机开发请求头</option></select></label>
    <label v-if="mode === 'token'">访问令牌 <input v-model="token" type="password" autocomplete="off" /></label>
    <template v-else><label>主体 <input v-model="subject" maxlength="128" /></label><label>租户 <input v-model="tenant" maxlength="64" /></label></template>
    <button @click="apply">应用读取身份并清空旧数据</button>
  </fieldset>
  <p role="status">{{ message }}</p>
  <fieldset :disabled="busy || !applied"><legend>Run 列表与定位</legend>
    <label>Agent 筛选 <input v-model="agent" maxlength="64" /></label><button @click="list()">刷新 Run 列表</button>
    <button :disabled="!cursor" @click="list(true)">加载下一页</button>
    <button v-if="linkedRun" @click="runId = linkedRun">定位链接中的 Run（尚未读取）</button>
    <label>Run ID <input v-model="runId" /></label><button @click="inspect(runId.trim())">读取指定 Run</button>
    <table border="1"><thead><tr><th>Run</th><th>Agent / 版本</th><th>状态</th><th>主体</th></tr></thead><tbody><tr v-for="row in rows" :key="row.run_id"><td><button @click="inspect(row.run_id)">{{ row.run_id }}</button></td><td>{{ row.agent_id }}@{{ row.agent_version }}</td><td>{{ row.state }}</td><td>{{ row.subject }}</td></tr></tbody></table>
  </fieldset>
  <section v-if="selected"><h3>{{ selected.run_id }} · {{ selected.state }}</h3>
    <RunOutcome :state="selected.state" :events="events" />
    <router-link :to="{ path: '/chat', query: { run: selected.run_id } }">返回此 Run 工作台（需重新应用身份）</router-link>
    <button :disabled="busy" @click="inspect(selected.run_id, true)">增量刷新轨迹</button>
    <button :disabled="busy" @click="inspect(selected.run_id)">重新完整读取</button>
    <p>序号 {{ events.at(-1)?.seq ?? 0 }} / 快照 last_seq {{ selected.last_seq }}。手动刷新；受限审批视图允许序号缺口，不推断为日志丢失。读取权限变化后请重新完整读取，补齐此前不可见的事件。</p>
    <p v-if="trace">最近一次执行 Trace：{{ trace }} <a v-if="traceUrl" :href="traceUrl" target="_blank" rel="noopener noreferrer">在本机 Langfuse 查看 Trace</a><span v-else>（配置 VITE_LANGFUSE_PROJECT_ID 与本机 VITE_LANGFUSE_HOST 后可跳转）</span></p>
    <p v-else>尚无可读取的 Trace 尾帧；运行中不猜测 Trace ID。一个 Run 续跑可能对应多条 Trace。</p>
    <p>轨迹可能含敏感数据，仅当前页面展示，不写浏览器存储；展开查看原始负载。</p>
    <ol><li v-for="event in events" :key="event.seq"><b>#{{ event.seq }} {{ event.type }}</b> · {{ event.ts }}
      <p v-if="event.type.startsWith('tool.')">调用 {{ event.payload.call_id }} · {{ event.payload.tool ?? toolNames.get(event.payload.call_id) }} · 判定 {{ event.payload.decision }} · 原因 {{ event.payload.reason }} · 状态 {{ event.payload.status }}</p>
      <p>阶段：{{ eventStage(event.type) }}</p>
      <details><summary>事件负载</summary><pre>{{ JSON.stringify(event.payload, null, 2) }}</pre></details>
    </li></ol>
  </section>
</template>
<style scoped>fieldset { margin:16px 0; } label, button { margin:6px; } table { width:100%; } pre { white-space:pre-wrap; overflow-wrap:anywhere; } li { padding:8px; border-bottom:1px solid #ddd; }</style>
