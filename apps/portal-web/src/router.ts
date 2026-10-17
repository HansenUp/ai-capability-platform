/** 门户路由：Agent 列表 → 选中后进入对话页；/callback 处理 Keycloak 登录回跳。未登录访问其他页面时先跳转登录（S3.4）。 */
import { createRouter, createWebHistory } from "vue-router";
import { isLoggedIn, login } from "./auth";

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", component: () => import("./views/AgentList.vue") },           // 可用 Agent 列表
    { path: "/chat/:agentId", component: () => import("./views/Chat.vue") },   // 与指定 Agent 对话
    { path: "/callback", component: () => import("./views/Callback.vue") },   // OIDC 登录回跳
  ],
});

router.beforeEach(async (to) => {
  if (to.path === "/callback" || isLoggedIn()) return true;
  await login(to.fullPath);
  return false;
});
