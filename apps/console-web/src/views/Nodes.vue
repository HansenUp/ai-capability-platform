<!-- 节点与路由：展示 Console API /routing 的路由表（节点组、绑定权重、默认组） -->
<script setup lang="ts">
import { onMounted, ref } from "vue";
const routing = ref<any>({}); // 路由表原始 JSON
onMounted(async () => { routing.value = await (await fetch("/console/routing")).json(); });
</script>
<template>
  <h2>节点与路由</h2>
  <pre>{{ JSON.stringify(routing, null, 2) }}</pre>
</template>
