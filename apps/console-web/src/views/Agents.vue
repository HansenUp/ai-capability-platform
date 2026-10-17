<!-- Agent 版本管理（Console API）：切换操作者身份 → 填写快照 → 保存并发布（可带灰度比例）→ 放量 / 一键回滚。
     开发态以 X-Console-* 请求头表示操作者（生产由登录态与 APISIX 注入）；列表按操作者租户来自 Console 库。 -->
<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";

const ROLES = ["USER", "ADMIN", "TENANT_ADMIN"] as const;
const saved = (() => { try { return JSON.parse(localStorage.getItem("console.operator") ?? "null"); } catch { return null; } })();
const op = reactive<{ user: string; role: string; tenant: string }>(saved ?? { user: "ops", role: "TENANT_ADMIN", tenant: "default" });
watch(op, () => { try { localStorage.setItem("console.operator", JSON.stringify(op)); } catch { /* 隐私模式下忽略 */ } load(); });
const H = computed(() => ({ "content-type": "application/json", "X-Console-User": op.user, "X-Console-Role": op.role, "X-Console-Tenant": op.tenant }));

const list = ref<any[]>([]);
const msg = ref("");
const form = reactive({ agent_id: "", version: "1", system_prompt: "", model_alias: "deepseek-v4-pro-2026-08", harness: "litellm", tools: "echo", skills: "", canary: 100 });

/** 调用 Console API，失败时把状态码与原因显示出来（便于观察 403 / 409 / 502）。 */
async function call(method: string, path: string, body?: unknown) {
  const res = await fetch(`/console${path}`, { method, headers: H.value, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await res.text();
  msg.value = res.ok ? `${method} ${path} → ${res.status}` : `${method} ${path} → ${res.status}：${text}`;
  return res.ok ? (text ? JSON.parse(text) : {}) : null;
}

async function load() { list.value = (await call("GET", "/agents")) ?? []; }

async function createAndPublish() {
  const config = { tools: form.tools.split(",").map((s) => s.trim()).filter(Boolean), skills: form.skills.split(",").filter(Boolean).map((name) => ({ name: name.trim() })) };
  const draft = { agent_id: form.agent_id, version: form.version, system_prompt: form.system_prompt, model_alias: form.model_alias, harness: form.harness || null, config };
  if (!(await call("POST", "/agents", draft))) return;
  await call("POST", `/agents/${form.agent_id}/versions/${form.version}/publish`, { eval_passed: true, eval_run_ref: "manual", canary_percent: Number(form.canary) });
  const keep = msg.value; await load(); msg.value = keep;
}

async function setCanary(a: any) {
  const v = prompt(`${a.agent_id}@${a.version} 放量比例（0～100）`, String(a.canary.percent));
  if (v === null) return;
  await call("PUT", `/agents/${a.agent_id}/versions/${a.version}/canary`, { percent: Number(v), allowlist: a.canary.allowlist });
  const keep = msg.value; await load(); msg.value = keep;
}

async function rollback(a: any) {
  if (!confirm(`回滚到 ${a.agent_id}@${a.version}？其他已发布版本将退役。`)) return;
  await call("POST", `/agents/${a.agent_id}/versions/${a.version}/rollback`);
  const keep = msg.value; await load(); msg.value = keep;
}

onMounted(load);
</script>
<template>
  <h2>Agent 版本</h2>
  <fieldset style="max-width:720px;margin-bottom:12px">
    <legend>当前操作者（开发态身份头）</legend>
    用户 <input v-model.lazy="op.user" style="width:90px" />
    角色 <select v-model="op.role"><option v-for="r in ROLES" :key="r">{{ r }}</option></select>
    租户 <input v-model.lazy="op.tenant" style="width:90px" />
  </fieldset>
  <form @submit.prevent="createAndPublish" style="display:grid;grid-template-columns:1fr 1fr;gap:8px;max-width:720px">
    <input v-model="form.agent_id" placeholder="agent_id" required /><input v-model="form.version" placeholder="version" />
    <input v-model="form.model_alias" placeholder="模型别名（版本锁定）" /><input v-model="form.harness" placeholder="harness：litellm / claude-sdk / mock" />
    <input v-model="form.tools" placeholder="核心工具，逗号分隔" /><input v-model="form.skills" placeholder="Skill 名称，逗号分隔" />
    <label>灰度比例 <input v-model="form.canary" type="number" min="0" max="100" style="width:70px" />%（100 为全量）</label>
    <textarea v-model="form.system_prompt" placeholder="人设 / 系统提示词" />
    <button>保存并发布（评测闸门手动放行）</button>
  </form>
  <p style="font-size:12px;color:#646a73">{{ msg }}</p>
  <table style="margin-top:8px;border-collapse:collapse" border="1" cellpadding="6">
    <tr><th>agent_id</th><th>version</th><th>status</th><th>灰度</th><th>model</th><th>harness</th><th>操作</th></tr>
    <tr v-for="a in list" :key="a.agent_id + a.version">
      <td>{{ a.agent_id }}</td><td>{{ a.version }}</td><td>{{ a.status }}</td><td>{{ a.canary.percent }}%</td><td>{{ a.model_alias }}</td><td>{{ a.harness ?? "节点默认" }}</td>
      <td>
        <button v-if="a.status === 'published'" @click="setCanary(a)">放量</button>
        <button v-if="a.status !== 'draft'" @click="rollback(a)">回滚到此版本</button>
      </td>
    </tr>
  </table>
</template>
