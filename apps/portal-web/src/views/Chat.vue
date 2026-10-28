<!-- Reuse the Console workbench and SDK ledger protocol; no legacy Messages or feedback writes. -->
<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import Workbench from "../../../console-web/src/views/Chat.vue";
import { getToken } from "../auth";
import { portalTokenKey } from "../runtime-session";
import { inject } from "vue";
const tokenProvider = inject(portalTokenKey, getToken);
const route = useRoute();
const agentId = computed(() => String(route.params.agentId ?? ""));
</script>
<template>
  <router-link to="/tasks">返回我的任务</router-link>
  <router-link to="/">返回可用 Agent</router-link>
  <h2>{{ agentId }}</h2>
  <Workbench :key="agentId" :portal-agent="agentId" :portal-token-provider="tokenProvider" />
</template>
