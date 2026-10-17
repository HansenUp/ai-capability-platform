/**
 * Console 路由：双工作台 —— /manage/*（管理侧；不用 /admin，避免与开发代理的 /admin → Runtime 冲突，刷新页面时被转发）与 /chat（使用侧）。
 * V1.2 范围内不含工作流画布。
 */
import { createRouter, createWebHistory } from "vue-router";
export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/manage/agents" },                                 // 默认进入 Agent 管理
    { path: "/manage/agents", component: () => import("./views/Agents.vue") }, // Agent 版本：创建 / 发布
    { path: "/manage/nodes", component: () => import("./views/Nodes.vue") },   // 节点与路由表
    { path: "/chat", component: () => import("./views/Chat.vue") },           // 使用侧：嵌入 <ai-assistant> 调试对话
  ],
});
