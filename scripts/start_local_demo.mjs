import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { join, resolve } from "node:path";
import { mkdtemp, readFile, writeFile, access, rm, mkdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { setTimeout as sleep } from "node:timers/promises";
const root = resolve(fileURLToPath(new URL("..", import.meta.url)));
const requireConsole = createRequire(join(root, "apps/console-web/package.json"));
const { createServer } = await import(requireConsole.resolve("vite"));
const { default: vue } = await import(requireConsole.resolve("@vitejs/plugin-vue"));
const temporary = await mkdtemp(join(tmpdir(), "ai-platform-demo-")), metadata = join(temporary, "fixture.json");
const ticketReadonly = process.argv.includes("--ticket-readonly");
const evidence = process.env.S12_DEMO_EVIDENCE || join(root, ticketReadonly ? "docs/evidence/s4/2026-10-27-readonly-demo" : "docs/evidence/s12/2026-10-26-local-demo");
const verifyOnly = process.argv.includes("--verify-only");
const safeEnv = { PATH: process.env.PATH, HOME: process.env.HOME, TMPDIR: temporary, LANG: "en_US.UTF-8", PYTHONUNBUFFERED: "1" };
let server, portal, fixture, tests, info, interrupted = false, fixtureLog = "", base;
const stop = () => { interrupted = true; tests?.kill("SIGTERM"); };
process.on("SIGINT", stop); process.on("SIGTERM", stop);
try {
  await mkdir(evidence, { recursive: true });
  fixture = spawn(join(root, ".venv/bin/python"), ["scripts/verify_s12_workbench.py", "--serve", "--browser-info", metadata, "--demo-no-migrations", ...(ticketReadonly ? ["--ticket-readonly-demo"] : [])],
    { cwd: root, env: safeEnv, stdio: ["ignore", "pipe", "pipe"] });
  fixture.stdout.on("data", value => { fixtureLog += value.toString(); }); fixture.stderr.on("data", value => { fixtureLog += value.toString(); });
  const deadline = Date.now() + 40000;
  while (!info) {
    if (interrupted || fixture.exitCode !== null || Date.now() >= deadline) throw new Error(`Demo fixture not ready: ${fixtureLog}`);
    try { info = JSON.parse(await readFile(metadata, "utf8")); } catch { await sleep(50); }
  }
  for (const address of [info.runtime, info.console]) if (new URL(address).hostname !== "127.0.0.1") throw new Error("Non-loopback demo refused");
  const tokens = JSON.parse(await readFile(info.tokens, "utf8")), expiresAt = Date.now() + 28 * 60000;
  let verified = null;
  server = await createServer({ configFile: false, envDir: temporary, cacheDir: join(temporary, "vite-cache"), root: join(root, "apps/console-web"), plugins: [vue(), {
    name: "isolated-demo-information",
    configureServer(vite) {
      vite.middlewares.use(async (request, response, next) => {
        const path = request.url?.split("?")[0];
        if (!["/demo", "/__demo/info"].includes(path)) return next();
        if (request.method !== "GET" || request.headers.host !== new URL(base).host
          || (request.headers.origin && request.headers.origin !== base)) { response.statusCode = 403; response.end(); return; }
        response.setHeader("Cache-Control", "no-store"); response.setHeader("Referrer-Policy", "no-referrer");
        response.setHeader("Content-Type", path === "/demo" ? "text/html; charset=utf-8" : "application/json");
        response.end(path === "/demo" ? await readFile(join(root, ticketReadonly ? "scripts/local_demo_ticket_readonly.html" : "scripts/local_demo.html"), "utf8")
          : JSON.stringify({ tokens: { alice: tokens.alice, bob: tokens.bob }, expires_at: new Date(expiresAt).toISOString(), verified, portal: info.portal }));
      });
    },
  }], define: { "import.meta.env.VITE_ISOLATED_DEMO": JSON.stringify("1"), "import.meta.env.VITE_LANGFUSE_HOST": JSON.stringify("http://127.0.0.1:1"), "import.meta.env.VITE_LANGFUSE_PROJECT_ID": JSON.stringify("") },
  server: { host: "127.0.0.1", port: 0, proxy: { "/v1": info.runtime,
    "/console": { target: info.console, rewrite: path => path.replace(/^\/console/, "") } } } });
  await new Promise((ready, reject) => { server.httpServer.once("error", reject); server.httpServer.listen(0, "127.0.0.1", ready); });
  base = `http://127.0.0.1:${server.httpServer.address().port}`;
  portal = await createServer({ configFile: false, envDir: temporary, cacheDir: join(temporary, "portal-vite-cache"), root: join(root, "apps/portal-web"), plugins: [vue(), {
    name: "isolated-portal-identity",
    configureServer(vite) {
      vite.middlewares.use((request, response, next) => {
        if (request.url?.split("?")[0] !== "/__demo/info") return next();
        const origin = `http://127.0.0.1:${portal.httpServer.address().port}`;
        if (request.method !== "GET" || request.headers.host !== new URL(origin).host || (request.headers.origin && request.headers.origin !== origin)) { response.statusCode = 403; response.end(); return; }
        response.setHeader("Cache-Control", "no-store"); response.setHeader("Content-Type", "application/json");
        response.end(JSON.stringify({ tokens: { alice: tokens.alice, bob: tokens.bob }, console: base + "/demo", expires_at: new Date(expiresAt).toISOString() }));
      });
    },
  }], define: { "import.meta.env.VITE_ISOLATED_DEMO": JSON.stringify("1") },
  server: { host: "127.0.0.1", port: 0, proxy: { "/v1": info.runtime, "/console": { target: info.console, rewrite: path => path.replace(/^\/console/, "") } } } });
  await new Promise((ready, reject) => { portal.httpServer.once("error", reject); portal.httpServer.listen(0, "127.0.0.1", ready); });
  const portalUrl = `http://127.0.0.1:${portal.httpServer.address().port}/demo.html`;
  info.portal = portalUrl;
  tests = spawn(process.execPath, ["--test", "--test-concurrency=1", "--test-timeout=30000", ...(ticketReadonly ? ["apps/console-web/tests/browser/ticket-readonly-demo.test.mjs"] : ["apps/console-web/tests/browser/a-demo-navigation.test.mjs", "apps/console-web/tests/browser/e2e.test.mjs", "apps/console-web/tests/browser/local-demo-trace.test.mjs"])],
    { cwd: root, env: { ...safeEnv, S12_BROWSER_URL: base, S12_RUNTIME_URL: info.runtime, S12_TEST_TOKEN_FILE: info.tokens,
      S12_BROWSER_EVIDENCE: evidence, PLAYWRIGHT_MODULE: process.env.PLAYWRIGHT_MODULE }, stdio: "inherit" });
  const [code] = await once(tests, "exit"); if (code !== 0) throw new Error("Demo main-chain browser validation failed");
  verified = JSON.parse(await readFile(join(evidence, "result.json"), "utf8"));
  await writeFile(join(temporary, "access.json"), JSON.stringify({ url: base + "/demo", expires_at: new Date(expiresAt).toISOString(), pid: process.pid }), { mode: 0o600 });
  console.log(`DEMO_READY ${base}/demo expires=${new Date(expiresAt).toISOString()} pid=${process.pid} portal=${portalUrl}`);
  while (!verifyOnly && !interrupted && Date.now() < expiresAt && fixture.exitCode === null) await sleep(500);
} catch (error) { console.error(error.message); process.exitCode = 1; }
finally {
  if (tests?.exitCode === null && tests.signalCode === null) { tests.kill("SIGTERM"); await once(tests, "exit"); }
  await portal?.close();
  await server?.close();
  if (fixture?.exitCode === null && fixture.signalCode === null) { fixture.kill("SIGINT"); await once(fixture, "exit"); }
  await mkdir(evidence, { recursive: true });
  await writeFile(join(evidence, "fixture.log"), fixtureLog);
  // Every child temporary file is under this invocation's private TMPDIR.
  // Remove it only after the owned processes have exited.
  await rm(temporary, { recursive: true, force: false });
  if (info) {
    try { await access(info.tokens); console.error("Demo test token file remains"); process.exitCode = 1; }
    catch { console.log("CLEANUP: demo stopped; ephemeral test token file removed; original databases untouched"); }
  }
  console.log("CLEANUP: owned demo temporary root removed");
  process.off("SIGINT", stop); process.off("SIGTERM", stop);
}
