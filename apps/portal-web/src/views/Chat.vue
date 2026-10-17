<!-- 对话页：从路由参数取 agentId，嵌入 <ai-assistant>；令牌由门户登录态提供，组件内置反馈控件（S3.5） -->
<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import { getToken } from "../auth";

const agentId = useRoute().params.agentId as string; // 路由 /chat/:agentId
const el = ref<HTMLElement & { tokenProvider?: () => Promise<string> }>();
onMounted(() => { if (el.value) el.value.tokenProvider = getToken; });
</script>
<template>
  <h2>{{ agentId }}</h2>
  <ai-assistant ref="el" :agent-id="agentId" base-url=""></ai-assistant>
</template>
