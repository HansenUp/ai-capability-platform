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
const version = ref("");
const versions = computed(() => [...new Set(agents.value.map(agent => agent.version))].sort());
const visible = computed(() => searchAgents(agents.value, query.value).filter(agent => !version.value || agent.version === version.value));
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
  <router-link to="/tasks">我的任务</router-link>
  <TaskGuide />
  <label>搜索 Agent <input v-model="query" /></label>
  <label for="catalog-version">筛选已发布版本</label><select id="catalog-version" v-model="version"><option value="">全部版本</option><option v-for="value in versions" :key="value" :value="value">{{ value }}</option></select>
  <p v-if="loading">正在读取已发布 Agent…</p>
  <p v-else-if="!error && !agents.length">当前身份没有可用的已发布 Agent。请联系管理员核对发布状态与可见范围；此页面不能扩大权限。</p>
  <section v-else-if="!error && !visible.length" aria-label="目录筛选无结果"><p>没有匹配的可用 Agent。</p><button type="button" @click="query = ''; version = ''">清除目录筛选</button></section>
  <p v-if="error" style="color:#b91c1c">{{ error }}</p>
  <ul class="agent-grid"><li v-for="a in visible" :key="a.agent_id"><span class="agent-icon" aria-hidden="true">✦</span><router-link :to="`/chat/${encodeURIComponent(a.agent_id)}`">{{ a.agent_id }}</router-link><span class="agent-meta">已发布 · 版本 {{ a.version }} · 独立 Run</span><span class="agent-meta">进入工作台 →</span><details><summary>查看 {{ a.agent_id }} 的详情</summary><dl><dt>Agent 标识</dt><dd>{{ a.agent_id }}</dd><dt>当前可用版本</dt><dd>{{ a.version }}</dd></dl><p>这是当前身份可见的已发布版本。实际执行版本以新任务的运行记录为准，列表不授予额外权限。</p><p>进入工作台后显式登记并执行新任务；已有任务请从“我的任务”定位原 Run，不会自动续聊或重试。</p><router-link to="/tasks">查看本人已有任务</router-link></details></li></ul>
</template>
