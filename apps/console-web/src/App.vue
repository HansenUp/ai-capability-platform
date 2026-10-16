<!-- Console 根组件：顶部导航 + 路由出口 -->
<template>
  <nav style="display:flex;gap:16px;padding:12px 20px;border-bottom:1px solid #e5e7eb">
    <b>Agent Console</b>
    <router-link to="/admin/agents">Agent</router-link>
    <router-link to="/admin/nodes">节点</router-link>
    <router-link to="/chat">聊天侧</router-link>
  </nav>
  <main style="max-width:1100px;margin:0 auto;padding:20px"><router-view /></main>
</template>
