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
const b3Read = process.argv.includes("--b3-read");
const f6Readonly = process.argv.includes("--f6-readonly");
const analyticsReadonly = f6Readonly || process.argv.includes("--analytics-readonly");
const salesReadonly = process.argv.includes("--sales-readonly");
const ticketReadonly = analyticsReadonly || salesReadonly || b3Read || process.argv.includes("--ticket-readonly");
const evidence = process.env.S12_DEMO_EVIDENCE || join(root, f6Readonly ? `docs/evidence/f6/local-demo-${Date.now()}` : ticketReadonly ? "docs/evidence/s4/2026-10-27-readonly-demo" : "docs/evidence/s12/2026-10-26-local-demo");
const verifyOnly = process.argv.includes("--verify-only");
const safeEnv = { PATH: process.env.PATH, HOME: process.env.HOME, TMPDIR: temporary, LANG: "en_US.UTF-8", PYTHONUNBUFFERED: "1" };
let server, portal, fixture, tests, info, interrupted = false, fixtureLog = "", base;
const stop = () => { interrupted = true; tests?.kill("SIGTERM"); };
process.on("SIGINT", stop); process.on("SIGTERM", stop);
try {
  await mkdir(evidence, { recursive: true });
  fixture = spawn(join(root, ".venv/bin/python"), ["scripts/verify_s12_workbench.py", "--serve", "--browser-info", metadata, "--demo-no-migrations", ...(ticketReadonly ? ["--ticket-readonly-demo"] : []), ...(b3Read ? ["--b3-read-demo"] : []), ...(analyticsReadonly ? ["--analytics-readonly-demo"] : [])],
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
          : JSON.stringify({ tokens: { alice: tokens.alice, bob: tokens.bob }, alert_tokens: { alice: tokens.alert_alice, bob: tokens.alert_bob }, policy_tokens: { policy_maintainer: tokens.policy_maintainer, policy_reviewer_a: tokens.policy_reviewer_a, policy_reviewer_b: tokens.policy_reviewer_b }, view_tokens: { view_maintainer: tokens.view_maintainer, view_reviewer_a: tokens.view_reviewer_a, view_reviewer_b: tokens.view_reviewer_b }, expires_at: new Date(expiresAt).toISOString(), verified, portal: info.portal, b3_read: b3Read, f6: f6Readonly, scenarios: ticketReadonly ? ["ticket", "sales", ...(analyticsReadonly ? ["orders"] : [])] : [] }));
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
  tests = spawn(process.execPath, ["--test", "--test-concurrency=1", "--test-timeout=30000", ...(f6Readonly ? ["apps/console-web/tests/browser/order-metrics-demo.test.mjs", "apps/console-web/tests/browser/sales-readonly-demo.test.mjs", "apps/console-web/tests/browser/ticket-readonly-demo.test.mjs"] : ticketReadonly ? [analyticsReadonly ? "apps/console-web/tests/browser/order-metrics-demo.test.mjs" : salesReadonly ? "apps/console-web/tests/browser/sales-readonly-demo.test.mjs" : "apps/console-web/tests/browser/ticket-readonly-demo.test.mjs"] : ["apps/console-web/tests/browser/demo-alerts.test.mjs", "apps/console-web/tests/browser/a-demo-navigation.test.mjs", "apps/console-web/tests/browser/e2e.test.mjs", "apps/console-web/tests/browser/local-demo-trace.test.mjs"])],
    { cwd: root, env: { ...safeEnv, S12_BROWSER_URL: base, S12_RUNTIME_URL: info.runtime, S12_TEST_TOKEN_FILE: info.tokens,
      S12_BROWSER_EVIDENCE: evidence, B3_READ_DEMO: b3Read ? "1" : "", F6_READONLY: f6Readonly ? "1" : "", PLAYWRIGHT_MODULE: process.env.PLAYWRIGHT_MODULE }, stdio: "inherit" });
  const [code] = await once(tests, "exit"); if (code !== 0) throw new Error("Demo main-chain browser validation failed");
  verified = JSON.parse(await readFile(join(evidence, "result.json"), "utf8"));
  if (f6Readonly) {
    verified = { scenarios: await Promise.all(["ticket", "sales", "orders"].map(async name => JSON.parse(await readFile(join(evidence, `${name}-result.json`), "utf8")))) };
    await writeFile(join(evidence, "f6-result.json"), JSON.stringify(verified, null, 2));
  }
  if (b3Read) {
    const contexts = fixtureLog.split("\n").filter(line => line.startsWith("B3_VERIFIED ")).map(line => JSON.parse(line.slice(12)));
    for (const runId of [verified.run_id, verified.failed_run_id]) {
      if (contexts.filter(row => row.run_id === runId && row.call_id === "ticket-read" && row.verified).length !== 1)
        throw new Error("B3 browser run lacks exactly one verified Gateway/MCP context");
    }
    await writeFile(join(evidence, "b3-contexts.json"), JSON.stringify(contexts, null, 2));
  }
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
