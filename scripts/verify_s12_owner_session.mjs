import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { join, resolve } from "node:path";
import { spawn } from "node:child_process";
import { once } from "node:events";
const root = resolve(fileURLToPath(new URL("..", import.meta.url)));
const requireConsole = createRequire(join(root, "apps/console-web/package.json"));
const { createServer } = await import(requireConsole.resolve("vite"));
const { default: vue } = await import(requireConsole.resolve("@vitejs/plugin-vue"));
let server, tests;
const stop = () => tests?.kill("SIGTERM");
process.on("SIGINT", stop); process.on("SIGTERM", stop);
try {
  server = await createServer({ configFile: false, root: join(root, "apps/console-web"), plugins: [vue()],
    server: { host: "127.0.0.1", port: 0 } });
  await new Promise((ready, reject) => { server.httpServer.once("error", reject); server.httpServer.listen(0, "127.0.0.1", ready); });
  tests = spawn(process.execPath, ["--test", "--test-concurrency=1", "--test-timeout=30000", "apps/console-web/tests/browser/owner-session.test.mjs"],
    { cwd: root, stdio: "inherit", env: { ...process.env, S12_OWNER_SESSION_URL: `http://127.0.0.1:${server.httpServer.address().port}` } });
  const [code] = await once(tests, "exit"); if (code !== 0) process.exitCode = 1;
} catch (error) { console.error(error.message); process.exitCode = 1; }
finally {
  if (tests?.exitCode === null) { tests.kill("SIGTERM"); await once(tests, "exit"); }
  await server?.close(); process.off("SIGINT", stop); process.off("SIGTERM", stop);
  console.log("CLEANUP: isolated static Vite fixture and browser stopped; no IdP or backend service started");
}
