<!-- 可用 Agent 列表：读取 Runtime /admin/agents，只展示已发布版本（生产按可见性过滤，来自 Console 的 visibility 配置） -->
<script setup lang="ts">
import { onMounted, ref } from "vue";
const agents = ref<{ agent_id: string; version: string; status: string }[]>([]);
onMounted(async () => { agents.value = (await (await fetch("/admin/agents")).json()).filter((a: any) => a.status === "published"); });
</script>
<template>
  <h2>可用 Agent</h2>
  <ul><li v-for="a in agents" :key="a.agent_id + a.version"><router-link :to="`/chat/${a.agent_id}`">{{ a.agent_id }}</router-link> · v{{ a.version }}</li></ul>
</template>
