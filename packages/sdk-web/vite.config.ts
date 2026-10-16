/** sdk-web 库模式构建：输出单个 ES 模块 dist/ai-assistant.js，供门户 / Console / 第三方页面引入。 */
import { defineConfig } from "vite";
export default defineConfig({
  build: { lib: { entry: "src/ai-assistant.ts", formats: ["es"], fileName: () => "ai-assistant.js" }, outDir: "dist" },
});
