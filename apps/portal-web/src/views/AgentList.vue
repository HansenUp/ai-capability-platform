<!-- 可用 Agent 列表：读取 Runtime /v1/agents（调用方租户内已发布的 Agent，以及按灰度规则该用户会用到的版本） -->
<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref } from "vue";
import TaskGuide from "../../../console-web/src/components/TaskGuide.vue";
import { getToken } from "../auth";
import { portalTokenKey } from "../runtime-session";
import { inject } from "vue";
const tokenProvider = inject(portalTokenKey, getToken);
import { readAgents, searchAgents, type PublishedAgent } from "../agent-catalog";

const agents = ref<PublishedAgent[]>([]);
const query = ref("");
const visible = computed(() => searchAgents(agents.value, query.value));
const error = ref("");
const loading = ref(true);
const controller = new AbortController();
onBeforeUnmount(() => controller.abort());
onMounted(async () => {
  try {
    const token = await tokenProvider();
    if (controller.signal.aborted) return;
    const rows = await readAgents(token, controller.signal);
    if (!controller.signal.aborted) agents.value = rows;
  } catch (reason) {
    if (!controller.signal.aborted) {
      agents.value = [];
      error.value = reason instanceof Error ? reason.message : "加载失败";
    }
  } finally {
    if (!controller.signal.aborted) loading.value = false;
  }
});
</script>
<template>
  <div class="page-kicker">YOUR AGENT WORKSPACE</div><h2>可用 Agent</h2><p>选择已发布的 Agent，开始一项新任务或继续查看已有 Run。</p>
  <TaskGuide />
  <label>搜索 Agent <input v-model="query" /></label>
  <p v-if="loading">正在读取已发布 Agent…</p>
  <p v-else-if="!error && !visible.length">没有匹配的可用 Agent。</p>
  <p v-if="error" style="color:#b91c1c">{{ error }}</p>
  <ul class="agent-grid"><li v-for="a in visible" :key="a.agent_id"><span class="agent-icon" aria-hidden="true">✦</span><router-link :to="`/chat/${encodeURIComponent(a.agent_id)}`">{{ a.agent_id }}</router-link><span class="agent-meta">已发布 · 版本 {{ a.version }} · 独立 Run</span><span class="agent-meta">进入工作台 →</span></li></ul>
</template>
