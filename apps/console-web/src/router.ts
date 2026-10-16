/**
 * Console 路由：双工作台 —— /admin/*（管理侧）与 /chat（使用侧）。
 * V1.2 范围内不含工作流画布。
 */
import { createRouter, createWebHistory } from "vue-router";
export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/admin/agents" },                                 // 默认进入 Agent 管理
    { path: "/admin/agents", component: () => import("./views/Agents.vue") }, // Agent 版本：创建 / 发布
    { path: "/admin/nodes", component: () => import("./views/Nodes.vue") },   // 节点与路由表
    { path: "/chat", component: () => import("./views/Chat.vue") },           // 使用侧：嵌入 <ai-assistant> 调试对话
  ],
});
