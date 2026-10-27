import "../../console-web/src/platform.css";
/**
 * 统一 AI 门户入口（Vue 3）。
 * 注册 Pinia 与路由；工作台使用 SDK 的独立 Run 客户端入口。
 */
import { createPinia } from "pinia";
import { createApp } from "vue";
import App from "./App.vue";
import { router } from "./router";

createApp(App).use(createPinia()).use(router).mount("#app");
