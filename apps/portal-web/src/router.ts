/** 门户路由：Agent 列表 → 选中后进入对话页。 */
import { createRouter, createWebHistory } from "vue-router";
export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", component: () => import("./views/AgentList.vue") },           // 可用 Agent 列表
    { path: "/chat/:agentId", component: () => import("./views/Chat.vue") },   // 与指定 Agent 对话
  ],
});
