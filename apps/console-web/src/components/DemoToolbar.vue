<script setup lang="ts">
import { inject } from "vue";
import { demoContextKey } from "../demo-context";
const demo = inject(demoContextKey)!;
</script>
<template>
  <aside aria-label="独立演示导航" style="padding:16px;background:#fff1cf;border-bottom:1px solid #ddd">
    <b>隔离演示 · 模拟模型与工具 · 非生产</b>
    <p>{{ demo.message.value }}</p>
    <label>演示测试主体 <select aria-label="演示测试主体" :value="demo.role.value" :disabled="!demo.ready.value" @change="demo.select(($event.target as HTMLSelectElement).value)">
      <option value="">未选择</option><option value="alice">Alice：发起／补充输入</option><option value="bob">Bob：他人审批</option>
    </select></label>
    <button @click="demo.load()">重读隔离演示信息</button>
    <label>演示Agent定位 <input v-model="demo.agent.value" maxlength="64"></label>
    <label>演示Run定位 <input v-model="demo.run.value" maxlength="128"></label>
    <p><a href="/demo" target="_blank" rel="noopener">完整体验说明</a> ·
      <router-link to="/manage/agents">① 配置与发布</router-link> →
      <router-link to="/chat">② 对话／审批／补充输入</router-link> →
      <router-link to="/manage/runs">③ 同Run只读轨迹</router-link> ·
      <router-link v-if="demo.alertsReady.value" to="/manage/alerts">④ 合成告警体验</router-link>
    </p>
    <small>切换主体会清空并卸载当前数据；新页面须显式应用身份。这里只保留内存定位，不授予管理角色，不解锁服务端未决写，不自动执行或确认回执。审批与本人ACK步骤保持不变。</small>
  </aside>
</template>
