/**
 * Console 前端 Vite 配置。
 * 开发代理：/v1、/admin → Runtime(:8080)；/console/* → Console API(:8081)，去掉 /console 前缀。
 */
import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vite";
export default defineConfig({ plugins: [vue()], server: { proxy: { "/v1": "http://localhost:8080", "/admin": "http://localhost:8080", "/console": { target: "http://localhost:8081", rewrite: (p) => p.replace(/^\/console/, "") } } } });
