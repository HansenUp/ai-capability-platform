<script setup lang="ts">
import { onBeforeUnmount, provide } from "vue";
import { useRoute } from "vue-router";
import { createDemoContext, demoContextKey } from "./demo-context";
import DemoToolbar from "./components/DemoToolbar.vue";
const route = useRoute();
const demo = import.meta.env.DEV && import.meta.env.VITE_ISOLATED_DEMO === "1" ? createDemoContext() : null;
if (demo) { provide(demoContextKey, demo); void demo.load(); }
onBeforeUnmount(() => demo?.dispose());
</script>
<template>
 <div class="console-shell">
  <nav class="side-nav" aria-label="主导航">
   <b class="brand">Agent Platform<span style="color:#7e9bff">.</span></b><small>WORKSPACE / CONSOLE</small>
   <router-link to="/manage/agents">Agent 配置与发布</router-link><router-link to="/chat">使用工作台</router-link>
   <router-link to="/a2a-host">A2A 隔离宿主</router-link><router-link to="/a2a-task">A2A 隔离结果</router-link><router-link to="/manage/runs">Run 调试与轨迹</router-link><router-link to="/manage/operations">运营只读</router-link>
   <router-link to="/manage/nodes">节点管理</router-link><router-link to="/manage/skills">Skill 审核</router-link>
   <router-link to="/manage/data-views">受限视图预览</router-link><router-link to="/manage/alerts">隔离告警</router-link><router-link to="/manage/directory">租户与用户目录</router-link><router-link to="/manage/owner-operations">Owner只读</router-link>
  </nav>
  <div class="console-content"><header class="topbar"><strong>工作空间 / Console</strong><span v-if="demo" class="badge">隔离演示 · 合成数据</span></header>
   <div class="page-body"><DemoToolbar v-if="demo" /><router-view :key="demo ? `${route.fullPath}:${demo.epoch.value}` : undefined" /></div>
  </div>
 </div>
</template>
