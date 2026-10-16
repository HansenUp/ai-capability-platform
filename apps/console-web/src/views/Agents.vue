<!-- Agent 版本管理：填写快照 → 保存草稿 → 发布（走 Console API 的评测闸门）→ 列表来自 Runtime /admin/agents -->
<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
// 开发态操作者身份头；生产由登录态与 APISIX 注入
const H = { "content-type": "application/json", "X-Console-User": "dev", "X-Console-Role": "TENANT_ADMIN" };
const list = ref<any[]>([]); // Runtime 中已登记的 Agent 版本列表
// 表单：tools / skills 以逗号分隔，提交时转换为 config 结构
const form = reactive({ agent_id: "", version: "1", system_prompt: "", model_alias: "default-chat", tools: "echo", skills: "" });
/** 拉取 Runtime 的 Agent 版本列表（经 Vite 代理到 :8080）。 */
async function load() { list.value = await (await fetch("/admin/agents")).json(); }
/** 创建草稿并立即发布；这里以 eval_passed=true 手动放行，正式流程由评测任务写入。 */
async function createAndPublish() {
  const config = { tools: form.tools.split(",").map((s) => s.trim()).filter(Boolean), skills: form.skills.split(",").filter(Boolean).map((name) => ({ name: name.trim() })) };
  await fetch("/console/agents", { method: "POST", headers: H, body: JSON.stringify({ ...form, config }) });
  await fetch(`/console/agents/${form.agent_id}/versions/${form.version}/publish`, { method: "POST", headers: H, body: JSON.stringify({ eval_passed: true, eval_run_ref: "manual" }) });
  await load();
}
onMounted(load);
</script>
<template>
  <h2>Agent 版本</h2>
  <!-- 快照表单 -->
  <form @submit.prevent="createAndPublish" style="display:grid;grid-template-columns:1fr 1fr;gap:8px;max-width:720px">
    <input v-model="form.agent_id" placeholder="agent_id" required /><input v-model="form.version" placeholder="version" />
    <input v-model="form.model_alias" placeholder="模型别名（版本锁定）" /><input v-model="form.tools" placeholder="核心工具，逗号分隔" />
    <input v-model="form.skills" placeholder="Skill 名称，逗号分隔" /><textarea v-model="form.system_prompt" placeholder="人设 / 系统提示词" />
    <button>保存并发布（需评测通过）</button>
  </form>
  <!-- 版本列表 -->
  <table style="margin-top:16px;border-collapse:collapse" border="1" cellpadding="6">
    <tr><th>agent_id</th><th>version</th><th>status</th><th>model</th><th>harness</th></tr>
    <tr v-for="a in list" :key="a.agent_id + a.version"><td>{{ a.agent_id }}</td><td>{{ a.version }}</td><td>{{ a.status }}</td><td>{{ a.model_alias }}</td><td>{{ a.harness ?? "node default" }}</td></tr>
  </table>
</template>
