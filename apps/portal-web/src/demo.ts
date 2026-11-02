import "../../console-web/src/platform.css";
/** Explicit local-only demo entry. Production main/router/OIDC remain unchanged. */
import { createApp, h, ref, onMounted, onUnmounted } from "vue";
import { createRouter, createWebHashHistory, RouterView } from "vue-router";
import { portalTokenKey } from "./runtime-session";
import AgentList from "./views/AgentList.vue";
import Tasks from "./views/Tasks.vue";
import Chat from "./views/Chat.vue";

if (import.meta.env.VITE_ISOLATED_DEMO !== "1" || location.hostname !== "127.0.0.1") throw new Error("Isolated demo only");
const restartGuide = "停止或到期后，请在启动终端核对清理完成，再使用原隔离启动命令开启新环境；使用新地址与新测试身份。刷新不能续期，旧Run与回执不迁移，不自动重发或解锁。";
try {
const response = await fetch("/__demo/info", { cache: "no-store" });
if (!response.ok) throw new Error("Demo identity unavailable");
const info = await response.json();
const expiresAt = Date.parse(info.expires_at);
if (!Number.isFinite(expiresAt) || !info.tokens?.alice || !info.tokens?.bob) throw new Error("Invalid demo information");
const expired = ref(Date.now() >= expiresAt);
function checkExpiry() {
  if (Date.now() >= expiresAt) {
    expired.value = true;
    info.tokens = {};
  }
}
checkExpiry();
const identity = ref<"alice" | "bob">("alice");
const selection = ref<"alice" | "bob">("alice");
const generation = ref(0);
const router = createRouter({ history: createWebHashHistory(), routes: [
  { path: "/tasks", component: Tasks },
  { path: "/", component: AgentList },
  { path: "/chat/:agentId", component: Chat },
] });
const app = createApp({ setup() {
  let timer: ReturnType<typeof setInterval>;
  onMounted(() => { if (!location.hash) void router.replace("/"); timer = setInterval(checkExpiry, 1000); });
  onUnmounted(() => clearInterval(timer));
  return () => h("main", { class: "portal-shell" }, [
    h("h1", "Portal · 隔离体验"),
    h("p", "临时库和签名测试身份；模型、工具、策略为替身。正式 OIDC 未接入。身份切换卸载旧页面，不解除服务端未决写锁。"),
    h("a", { href: info.console }, "返回统一 Console / Portal 体验入口"),
    h("p", `有效至 ${new Date(info.expires_at).toLocaleString()}`),
    h("section", { "aria-label": "演示到期与重启" }, [h("h2", "停止或到期后的处理"), h("p", restartGuide)]),
    ...(expired.value ? [h("p", { role: "alert" }, "演示身份已到期，已隐藏任务页面和测试身份操作。请重新启动独立演示。")] : [
    h("select", { "aria-label": "Portal测试主体", value: selection.value, onChange: (e: Event) => { selection.value = (e.target as HTMLSelectElement).value as "alice" | "bob"; } }, [h("option", { value: "alice" }, "Alice（发起人）"), h("option", { value: "bob" }, "Bob（审批人）")]),
    h("button", { onClick: () => { identity.value = selection.value; generation.value++; } }, "应用测试身份"),
    h("p", `当前身份：${identity.value}；跨身份查看同 Run 时请显式填写 Run ID。`),
    h(RouterView, { key: generation.value }),
    ]),
  ]);
} });
app.provide(portalTokenKey, async () => {
  checkExpiry();
  if (expired.value) throw new Error("演示身份已到期");
  return info.tokens[identity.value];
});
app.use(router).mount("#app");

} catch {
  createApp({render: () => h("main", {class: "portal-shell"}, [
    h("h1", "Portal · 隔离体验"),
    h("p", {role: "alert"}, "演示身份信息不可用：可能已停止、到期或启动失败。未加载任务或测试身份，不会自动重试。"),
    h("p", restartGuide),
    h("p", "检查启动终端的DEMO_CONFIG提示；前检通过后仍需等待DEMO_READY。不要改用真实服务或粘贴真实凭据。"),
  ])}).mount("#app");
}
