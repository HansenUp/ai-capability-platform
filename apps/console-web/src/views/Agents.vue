<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import AgentConfig from "../components/AgentConfig.vue";
import { configObject, differences } from "../agent-config";

interface AgentVersion {
  agent_id: string; version: string; status: string; system_prompt: string; model_alias: string;
  harness: string | null; config: Record<string, unknown>; eval_run_ref: string | null;
  canary: { percent: number; allowlist: string[] };
}
interface Gate { mode: string; min_items: number; min_pass_rate: number; datasets: string[] }
interface Evaluation {
  status: string; reason: string | null; passed_items: number | null; total_items: number | null;
  pass_rate: number | null; source: string; checked_at: string; eval_run_ref: string | null;
  stored_eval_run_ref: string | null; policy: Gate;
}
interface Action { method: string; path: string; body?: unknown; description: string }
const saved = (() => { try { return JSON.parse(localStorage.getItem("console.operator") ?? "null"); } catch { return null; } })();
const op = reactive<{ user: string; role: string; tenant: string }>(saved ?? { user: "ops", role: "TENANT_ADMIN", tenant: "default" });
const identity = reactive({ ...op });
const rows = ref<AgentVersion[]>([]);
const selected = ref<AgentVersion | null>(null);
const gate = ref<Gate | null>(null);
const evaluation = ref<Evaluation | null>(null);
const busy = ref(false);
const message = ref("");
const pending = ref<Action | null>(null);
const comparisonAgent = ref("");
const beforeVersion = ref("");
const afterVersion = ref("");
const comparisonRows = computed(() => rows.value.filter(row => row.agent_id === comparisonAgent.value));
const comparison = computed(() => {
  const before = comparisonRows.value.find(row => row.version === beforeVersion.value);
  const after = comparisonRows.value.find(row => row.version === afterVersion.value);
  const snapshot = (row: AgentVersion) => ({ system_prompt: row.system_prompt, model_alias: row.model_alias, harness: row.harness, config: row.config });
  return before && after ? differences(snapshot(before), snapshot(after)) : null;
});
const draft = reactive({ agent_id: "", version: "1", system_prompt: "", model_alias: "default-chat", harness: "mock", config: "{}" });
const release = reactive({ reference: "", percent: 100, allowlist: "", developmentPass: false });
const canManage = computed(() => ["ADMIN", "TENANT_ADMIN", "PLATFORM_ADMIN"].includes(op.role));
const locked = computed(() => busy.value || pending.value !== null || !canManage.value);
const pathOf = (row: AgentVersion) => `/agents/${encodeURIComponent(row.agent_id)}/versions/${encodeURIComponent(row.version)}`;
const principals = () => [...new Set(release.allowlist.split(/[\n,]/).map(value => value.trim()).filter(Boolean))];
let generation = 0;

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(`/console${path}`, {
    method, cache: "no-store", headers: { "Content-Type": "application/json", "X-Console-User": op.user, "X-Console-Role": op.role, "X-Console-Tenant": op.tenant },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  if (!response.ok) throw new Error(`${response.status} ${text}`);
  return (text ? JSON.parse(text) : null) as T;
}

async function perform(action: (current: number) => Promise<void>) {
  if (busy.value) return;
  const current = generation;
  busy.value = true;
  message.value = "";
  try { await action(current); }
  catch (error) { if (current === generation) message.value = String(error); }
  finally { if (current === generation) busy.value = false; }
}

async function refresh(current: number) {
  const result = await request<AgentVersion[]>("GET", "/agents");
  if (current !== generation) return;
  rows.value = result;
  if (selected.value) selected.value = result.find(row => row.agent_id === selected.value?.agent_id && row.version === selected.value?.version) ?? null;
}
async function load() { await perform(refresh); }

function applyIdentity() {
  if (busy.value || pending.value) return;
  generation += 1;
  Object.assign(op, identity);
  rows.value = [];
  comparisonAgent.value = beforeVersion.value = afterVersion.value = "";
  selected.value = null;
  gate.value = null;
  evaluation.value = null;
  Object.assign(draft, { agent_id: "", version: "1", system_prompt: "", model_alias: "default-chat", harness: "mock", config: "{}" });
  Object.assign(release, { reference: "", percent: 100, allowlist: "", developmentPass: false });
  try { localStorage.setItem("console.operator", JSON.stringify(op)); } catch {}
  void load();
}

async function inspect(row: AgentVersion) {
  selected.value = row;
  gate.value = null;
  evaluation.value = null;
  Object.assign(release, { reference: row.eval_run_ref ?? "", percent: row.canary.percent, allowlist: row.canary.allowlist.join("\n"), developmentPass: false });
  await perform(async current => {
    const result = await request<Gate>("GET", `/agents/${encodeURIComponent(row.agent_id)}/eval-gate`);
    if (current === generation) gate.value = result;
  });
}

function editDraft(row: AgentVersion) {
  selected.value = null;
  gate.value = null;
  evaluation.value = null;
  Object.assign(draft, { agent_id: row.agent_id, version: row.status === "draft" ? row.version : "", system_prompt: row.system_prompt,
    model_alias: row.model_alias, harness: row.harness ?? "", config: JSON.stringify(row.config, null, 2) });
  message.value = row.status === "draft" ? "仅编辑草稿；保存不会发布。" : "已复制配置，请填写新版本；已发布快照不可修改。";
}

async function saveDraft() {
  await perform(async current => {
    const config = configObject(draft.config);
    await request("POST", "/agents", { ...draft, harness: draft.harness || null, config });
    selected.value = null;
    gate.value = null;
    evaluation.value = null;
    await refresh(current);
    if (current === generation) message.value = "草稿已保存，未发布、未调用 Runtime 发布接口。";
  });
}

async function inspectEvaluation() {
  const row = selected.value;
  if (!row) return;
  evaluation.value = null;
  const reference = release.reference.trim();
  await perform(async current => {
    const query = reference ? `?${new URLSearchParams({ eval_run_ref: reference })}` : "";
    const result = await request<Evaluation>("GET", pathOf(row) + "/evaluation" + query);
    if (current === generation && selected.value === row) evaluation.value = result;
  });
}

function prepare(kind: "publish" | "canary" | "rollback") {
  const row = selected.value;
  if (!row || !gate.value || busy.value || pending.value) return;
  try {
    const percent = Number(release.percent);
    const allowlist = principals();
    if (!Number.isInteger(percent) || percent < 0 || percent > 100) throw new Error("灰度比例必须是 0–100 整数");
    if (kind !== "rollback" && percent === 0 && !allowlist.length) throw new Error("0% 预发布必须有评测白名单");
    const requiresEvaluation = kind === "publish" ? percent > 0 : kind === "canary" && row.canary.percent === 0 && percent > 0;
    if (requiresEvaluation && gate.value.mode === "langfuse" && !release.reference.trim().startsWith("langfuse:"))
      throw new Error("请填写真实 Langfuse 数据集运行引用");
    if (requiresEvaluation && gate.value.mode === "flag" && !release.developmentPass)
      throw new Error("开发 flag 模式须显式确认评测通过；不等同真实评测验收");
    if (kind === "canary" && requiresEvaluation && gate.value.mode === "langfuse" && release.reference.trim() !== (row.eval_run_ref ?? ""))
      throw new Error("放量使用该版本已保存的评测引用；如需新引用，请重新走发布并验证");
    let action: Action;
    if (kind === "rollback") {
      if (row.status === "draft" || row.canary.percent === 0) throw new Error("草稿或 0% 预发布不能直接回滚为全量版本");
      action = { method: "POST", path: pathOf(row) + "/rollback",
        description: `回滚到 ${row.agent_id}@${row.version}，设为 100%，同 Agent 其他已发布版本将退役。` };
    } else {
      const body = kind === "publish"
        ? { eval_run_ref: release.reference.trim() || null, ...(gate.value.mode === "flag" ? { eval_passed: release.developmentPass } : {}),
            canary_percent: percent, canary_allowlist: allowlist }
        : { percent, allowlist };
      action = { method: kind === "publish" ? "POST" : "PUT", path: pathOf(row) + (kind === "publish" ? "/publish" : "/canary"), body,
        description: `${kind === "publish" ? "发布" : "调整灰度"} ${row.agent_id}@${row.version} 至 ${percent}%，白名单 ${allowlist.length} 个主体。${percent === 0 ? "仅白名单预发布，不代表通过评测。" : "后端评测闸门仍是最终判定。"}` };
    }
    pending.value = action;
    message.value = "";
  } catch (error) { message.value = String(error); }
}

async function confirmAction() {
  const action = pending.value;
  if (!action || busy.value) return;
  await perform(async current => {
    await request(action.method, action.path, action.body);
    pending.value = null;
    await refresh(current);
    if (current === generation) message.value = "操作成功，已刷新服务端状态；此页面不是生产部署入口。";
  });
}
onMounted(load);
</script>

<template>
  <h2>Agent 草稿、评测与发布</h2>
  <p>仅本机功能。保存草稿不发布；发布／放量由服务端评测闸门最终裁决，不代表生产部署或外部验收。</p>
  <fieldset :disabled="busy || pending !== null">
    <legend>开发态身份（不是生产鉴权） · 当前 {{ op.tenant }} / {{ op.user }} / {{ op.role }}</legend>
    <form @submit.prevent="applyIdentity">
      <label>用户 <input v-model="identity.user" required maxlength="128" /></label>
      <label>角色 <select v-model="identity.role"><option v-for="role in ['USER','ADMIN','TENANT_ADMIN','PLATFORM_ADMIN']" :key="role">{{ role }}</option></select></label>
      <label>租户 <input v-model="identity.tenant" required maxlength="64" /></label>
      <button>应用开发身份</button>
    </form>
  </fieldset>
  <p role="status">{{ message }}</p>
  <button :disabled="busy || pending !== null" @click="load">刷新版本</button>
  <fieldset :disabled="locked">
    <legend>保存草稿（不发布）</legend>
    <form @submit.prevent="saveDraft">
      <label>Agent ID <input v-model="draft.agent_id" required maxlength="64" /></label>
      <label>版本 <input v-model="draft.version" required maxlength="32" /></label>
      <label>模型别名 <input v-model="draft.model_alias" required maxlength="64" /></label>
      <label>Harness <input v-model="draft.harness" maxlength="32" /></label>
      <label class="block">系统提示词 <textarea v-model="draft.system_prompt" /></label>
      <AgentConfig v-model="draft.config" />
      <button>仅保存草稿</button>
    </form>
  </fieldset>
  <table border="1" cellpadding="6">
    <thead><tr><th>Agent</th><th>版本</th><th>状态</th><th>灰度</th><th>操作</th></tr></thead>
    <tbody><tr v-for="row in rows" :key="JSON.stringify([row.agent_id,row.version])">
      <td>{{ row.agent_id }}</td><td>{{ row.version }}</td><td>{{ row.status }}</td><td>{{ row.canary.percent }}%</td>
      <td><button :disabled="locked" @click="editDraft(row)">{{ row.status === 'draft' ? '编辑草稿' : '复制为新草稿' }}</button>
        <button :disabled="locked" @click="inspect(row)">评测与发布</button></td>
    </tr></tbody>
  </table>
  <section>
    <h3>版本配置对比（只读）</h3>
    <label>对比 Agent <select v-model="comparisonAgent" :disabled="busy || pending !== null" @change="beforeVersion = afterVersion = ''"><option value="">请选择</option><option v-for="id in [...new Set(rows.map(row => row.agent_id))]" :key="id">{{ id }}</option></select></label>
    <label>基准版本 <select v-model="beforeVersion"><option value="">请选择</option><option v-for="row in comparisonRows" :key="row.version">{{ row.version }}</option></select></label>
    <label>目标版本 <select v-model="afterVersion"><option value="">请选择</option><option v-for="row in comparisonRows" :key="row.version">{{ row.version }}</option></select></label>
    <p>比较人设、模型、Harness 与完整配置；不比较状态、灰度或评测引用，不修改任何快照。数组保留顺序。</p>
    <p v-if="comparison && !comparison.length">配置一致</p>
    <table v-if="comparison?.length" border="1"><thead><tr><th>字段（JSON Pointer）</th><th>基准</th><th>目标</th></tr></thead><tbody><tr v-for="item in comparison" :key="item.path"><td>{{ item.path }}</td><td><pre>{{ item.before }}</pre></td><td><pre>{{ item.after }}</pre></td></tr></tbody></table>
  </section>
  <section v-if="selected">
    <h3>{{ selected.agent_id }}@{{ selected.version }} · {{ selected.status }}</h3>
    <p v-if="gate">闸门 {{ gate.mode }} · 最少 {{ gate.min_items }} 条 · 通过率至少 {{ Math.round(gate.min_pass_rate * 100) }}% · 数据集 {{ gate.datasets.join(', ') || '未配置' }}</p>
    <p v-if="gate?.mode === 'flag'">当前为开发 flag 模式，不会伪称 Langfuse 真实评测通过。</p>
    <fieldset :disabled="locked || !gate">
      <legend>发布／灰度参数</legend>
      <label class="block">真实评测引用 <input v-model="release.reference" placeholder="langfuse:dataset/run" @input="evaluation = null" /></label>
      <button @click="inspectEvaluation">只读重算评测结果</button>
      <label>灰度比例 <input v-model.number="release.percent" type="number" min="0" max="100" step="1" required /></label>
      <label class="block">灰度白名单（每行或逗号分隔）<textarea v-model="release.allowlist" rows="3" /></label>
      <label v-if="gate?.mode === 'flag'"><input v-model="release.developmentPass" type="checkbox" />显式确认开发态评测通过（非真实评测验收）</label>
      <button @click="prepare('publish')">检查并发布</button>
      <button v-if="selected.status === 'published'" @click="prepare('canary')">检查并调整灰度</button>
      <button v-if="selected.status !== 'draft' && selected.canary.percent > 0" @click="prepare('rollback')">检查回滚</button>
    </fieldset>
    <section v-if="evaluation" aria-label="评测结果只读视图">
      <h4>当前策略重算 · {{ evaluation.status }}</h4>
      <p>引用 {{ evaluation.eval_run_ref ?? '未设置' }} · 已保存引用 {{ evaluation.stored_eval_run_ref ?? '未设置' }}</p>
      <p>通过条目 {{ evaluation.passed_items ?? '未知' }} / 总条目 {{ evaluation.total_items ?? '未知' }} · 通过率 {{ evaluation.pass_rate === null ? '未知' : `${(evaluation.pass_rate * 100).toFixed(1)}%` }}</p>
      <p>重算策略：{{ evaluation.policy.mode }} · 最少 {{ evaluation.policy.min_items }} 条 · 通过率至少 {{ (evaluation.policy.min_pass_rate * 100).toFixed(1) }}% · 来源 {{ evaluation.source }}</p>
      <p>读取时间 {{ evaluation.checked_at }} · {{ evaluation.reason ?? '当前策略重算通过' }}</p>
      <p>只读结果不是历史发布证明、不会发布／放量；实际变更仍须确认并由服务端重新执行发布闸门。开发 flag 不提供真实评测统计，缺失数据不补零。</p>
    </section>
  </section>
  <section v-if="pending" role="alert">
    <h3>确认变更</h3><p>{{ pending.description }}</p>
    <pre>{{ JSON.stringify(pending.body ?? {}, null, 2) }}</pre>
    <button :disabled="busy" @click="confirmAction">确认执行上述变更</button>
    <button :disabled="busy" @click="pending = null">取消变更</button>
  </section>
</template>
<style scoped>
fieldset { margin: 16px 0; }
label { display: inline-block; margin: 6px; }
.block { display: block; }
.block input, textarea { width: 95%; }
button { margin: 4px; }
table { width: 100%; border-collapse: collapse; }
pre { overflow-wrap: anywhere; white-space: pre-wrap; }
</style>
