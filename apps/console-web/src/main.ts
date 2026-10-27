import "./platform.css";
/**
 * Agent Console 前端入口（Vue 3）。
 * 注册 Pinia 状态、路由，并引入 `@ai-platform/sdk-web` 以注册 `<ai-assistant>` 自定义元素。
 */
import { createPinia } from "pinia";
import { createApp } from "vue";
import App from "./App.vue";
import { router } from "./router";
import "@ai-platform/sdk-web"; // 副作用导入：注册 <ai-assistant> 组件

createApp(App).use(createPinia()).use(router).mount("#app");
