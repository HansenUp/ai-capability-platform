<!-- 门户根组件：页头（当前用户 / 退出）+ 路由出口 -->
<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import { currentUser, logout } from "./auth";

const route = useRoute();
const user = computed(() => (route.path ? currentUser() : null)); // 随路由变化刷新（登录回跳后）
</script>
<template>
  <header class="portal-header">
    <span>统一 AI 门户</span>
    <span v-if="user" style="font-weight:400">{{ user.name }} · {{ user.tenant }} <button @click="logout">退出</button></span>
  </header>
  <main class="portal-shell"><router-view /></main>
</template>
