<!-- 对话页：从路由参数取 agentId，嵌入 <ai-assistant> -->
<script setup lang="ts">
import { useRoute } from "vue-router";
const agentId = useRoute().params.agentId as string; // 路由 /chat/:agentId
</script>
<template>
  <h2>{{ agentId }}</h2>
  <ai-assistant :agent-id="agentId" base-url=""></ai-assistant>
  <p style="color:#646a73;font-size:12px">反馈控件（点赞 / 点踮 / 修正）在 S3 接入 observe-adapter /feedback。</p>
</template>
