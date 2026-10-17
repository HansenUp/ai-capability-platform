<!-- 可用 Agent 列表：读取 Runtime /v1/agents（调用方租户内已发布的 Agent，以及按灰度规则该用户会用到的版本） -->
<script setup lang="ts">
import { onMounted, ref } from "vue";
import { getToken } from "../auth";

const agents = ref<{ agent_id: string; version: string; rollout: string }[]>([]);
const error = ref("");
onMounted(async () => {
  let token: string;
  try { token = await getToken(); } catch { return; } // 登录失效：getToken 已跳转登录
  const res = await fetch("/v1/agents", { headers: { authorization: `Bearer ${token}` } });
  if (res.ok) agents.value = await res.json();
  else error.value = `加载 Agent 列表失败：HTTP ${res.status}`;
});
</script>
<template>
  <h2>可用 Agent</h2>
  <p v-if="error" style="color:#b91c1c">{{ error }}</p>
  <ul><li v-for="a in agents" :key="a.agent_id"><router-link :to="`/chat/${a.agent_id}`">{{ a.agent_id }}</router-link> · v{{ a.version }}</li></ul>
</template>
