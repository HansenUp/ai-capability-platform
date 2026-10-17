<!-- OIDC 登录回跳：用授权码换取令牌后回到登录前的页面 -->
<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { handleCallback } from "../auth";

const error = ref("");
const router = useRouter();
onMounted(async () => {
  try {
    await router.replace(await handleCallback(window.location.search));
  } catch (e) {
    error.value = String(e);
  }
});
</script>
<template>
  <p v-if="error" style="color:#b91c1c">登录失败：{{ error }}</p>
  <p v-else>正在登录…</p>
</template>
