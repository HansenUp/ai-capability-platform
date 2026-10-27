import "../../console-web/src/platform.css";
/** Explicit local-only demo entry. Production main/router/OIDC remain unchanged. */
import { createApp, h, ref, onMounted } from "vue";
import { createRouter, createWebHashHistory, RouterView } from "vue-router";
import { portalTokenKey } from "./runtime-session";
import AgentList from "./views/AgentList.vue";
import Chat from "./views/Chat.vue";

if (import.meta.env.VITE_ISOLATED_DEMO !== "1" || location.hostname !== "127.0.0.1") throw new Error("Isolated demo only");
const response = await fetch("/__demo/info", { cache: "no-store" });
if (!response.ok) throw new Error("Demo identity unavailable");
const info = await response.json();
const identity = ref<"alice" | "bob">("alice");
const selection = ref<"alice" | "bob">("alice");
const generation = ref(0);
const router = createRouter({ history: createWebHashHistory(), routes: [
  { path: "/", component: AgentList },
  { path: "/chat/:agentId", component: Chat },
] });
const app = createApp({ setup() {
  onMounted(() => { if (!location.hash) void router.replace("/"); });
  return () => h("main", { class: "portal-shell" }, [
    h("h1", "Portal · 隔离体验"),
    h("p", "临时库和签名测试身份；模型、工具、策略为替身。正式 OIDC 未接入。身份切换卸载旧页面，不解除服务端未决写锁。"),
    h("a", { href: info.console }, "返回统一 Console / Portal 体验入口"),
    h("p", `有效至 ${new Date(info.expires_at).toLocaleString()}`),
    h("select", { value: selection.value, onChange: (e: Event) => { selection.value = (e.target as HTMLSelectElement).value as "alice" | "bob"; } }, [h("option", { value: "alice" }, "Alice（发起人）"), h("option", { value: "bob" }, "Bob（审批人）")]),
    h("button", { onClick: () => { identity.value = selection.value; generation.value++; } }, "应用测试身份"),
    h("p", `当前身份：${identity.value}；跨身份查看同 Run 时请显式填写 Run ID。`),
    h(RouterView, { key: generation.value }),
  ]);
} });
app.provide(portalTokenKey, async () => {
  if (Date.now() >= Date.parse(info.expires_at)) throw new Error("演示身份已到期");
  return info.tokens[identity.value];
});
app.use(router).mount("#app");
