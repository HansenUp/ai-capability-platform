import { spawn } from "node:child_process";
import { mkdtemp, readFile, stat, access } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout } from "node:timers/promises";
import { once } from "node:events";
import { createRequire } from "node:module";
const root = resolve(fileURLToPath(new URL("..", import.meta.url)));
const requireConsole = createRequire(join(root, "apps/console-web/package.json"));
const { createServer } = await import(requireConsole.resolve("vite"));
const { default: vue } = await import(requireConsole.resolve("@vitejs/plugin-vue"));
const temporary = await mkdtemp(join(tmpdir(), "s12-browser-"));
const metadata = join(temporary, "fixture.json");
const noMigrations = process.argv.includes("--no-migrations");
const safeEnv = noMigrations ? { PATH: process.env.PATH, HOME: process.env.HOME, TMPDIR: process.env.TMPDIR, LANG: "en_US.UTF-8" } : process.env;
const fixture = spawn(join(root, ".venv/bin/python"), ["scripts/verify_s12_workbench.py", "--serve", "--browser-info", metadata, ...(noMigrations ? ["--demo-no-migrations"] : [])],
  { cwd: root, env: safeEnv, stdio: ["ignore", "pipe", "pipe"] });
let fixtureLog = "", server, tests, info, interrupted = false;
function interrupt() { interrupted = true; tests?.kill("SIGTERM"); }
process.on("SIGINT", interrupt);
process.on("SIGTERM", interrupt);
fixture.stdout.on("data", data => { fixtureLog += data.toString(); });
fixture.stderr.on("data", data => { fixtureLog += data.toString(); });
async function ready() {
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    if (interrupted) throw new Error("Browser acceptance interrupted");
    if (fixture.exitCode !== null) throw new Error(`Isolated fixture failed: ${fixtureLog}`);
    try { await stat(metadata); return JSON.parse(await readFile(metadata, "utf8")); } catch {}
    await setTimeout(50);
  }
  throw new Error("Isolated fixture readiness timed out");
}
try {
  info = await ready();
  for (const url of [info.runtime, info.console]) if (new URL(url).hostname !== "127.0.0.1") throw new Error("Refusing non-loopback fixture");
  server = await createServer({ configFile: false, envDir: temporary, cacheDir: join(temporary, "vite-cache"), root: join(root, "apps/console-web"), plugins: [vue()],
    define: { "import.meta.env.VITE_LANGFUSE_HOST": JSON.stringify("http://127.0.0.1:3000"),
      "import.meta.env.VITE_LANGFUSE_PROJECT_ID": JSON.stringify("synthetic-project") },
    server: { host: "127.0.0.1", port: 0, proxy: { "/v1": info.runtime,
      "/console": { target: info.console, rewrite: path => path.replace(/^\/console/, "") } } } });
  await new Promise((ready, reject) => { server.httpServer.once("error", reject); server.httpServer.listen(0, "127.0.0.1", ready); });
  if (interrupted) throw new Error("Browser acceptance interrupted");
  const port = server.httpServer.address().port;
  console.log("PROFILE: isolated headless browser; loopback Console/Runtime; signed test JWT; synthetic tools; no trace/video/token recording");
  const testFiles = process.argv.includes("--portal-all") ? ["apps/console-web/tests/browser/component.test.mjs", "apps/console-web/tests/browser/portal-workbench.test.mjs", "apps/console-web/tests/browser/portal-catalog.test.mjs", "apps/console-web/tests/browser/portal-flow.test.mjs"] : process.argv.includes("--portal-flow") ? ["apps/console-web/tests/browser/portal-catalog.test.mjs", "apps/console-web/tests/browser/portal-flow.test.mjs"] : process.argv.includes("--portal-adapter") ? ["apps/console-web/tests/browser/portal-workbench.test.mjs", "apps/console-web/tests/browser/component.test.mjs"] : [
    "apps/console-web/tests/browser/component.test.mjs", "apps/console-web/tests/browser/owner-operations.test.mjs", "apps/console-web/tests/browser/publishing.test.mjs",
    "apps/console-web/tests/browser/nodes.test.mjs", "apps/console-web/tests/browser/readonly.test.mjs",
    "apps/console-web/tests/browser/directory-skills.test.mjs", "apps/console-web/tests/browser/agent-config.test.mjs",
    "apps/console-web/tests/browser/s12-closure.test.mjs", "apps/console-web/tests/browser/sse.test.mjs",
    "apps/console-web/tests/browser/credential.test.mjs", "apps/console-web/tests/browser/ledger.test.mjs", "apps/console-web/tests/browser/approval.test.mjs", "apps/console-web/tests/browser/answer.test.mjs", "apps/console-web/tests/browser/create.test.mjs", "apps/console-web/tests/browser/feedback.test.mjs", "apps/console-web/tests/browser/feedback-v2.test.mjs", "apps/console-web/tests/browser/trusted-feedback.test.mjs", "apps/console-web/tests/browser/e2e.test.mjs"];
  if (process.argv.includes("--with-portal")) testFiles.push("apps/console-web/tests/browser/portal-workbench.test.mjs", "apps/console-web/tests/browser/portal-catalog.test.mjs", "apps/console-web/tests/browser/portal-flow.test.mjs");
  tests = spawn(process.execPath, ["--test", "--test-concurrency=1", "--test-timeout=30000", ...testFiles], {
    cwd: root, stdio: "inherit", env: { ...process.env, S12_BROWSER_URL: `http://127.0.0.1:${port}`,
      S12_RUNTIME_URL: info.runtime, S12_TEST_TOKEN_FILE: info.tokens, S12_CREDENTIAL_RUNS: JSON.stringify(info.credential_runs), S12_FEEDBACK_FIXTURE: JSON.stringify(info.feedback) },
  });
  const [code] = await once(tests, "exit");
  if (code !== 0) throw new Error(`Browser acceptance failed, exit ${code}`);
} catch (error) { console.error(error.message, fixtureLog); process.exitCode = 1; }
finally {
  if (tests?.exitCode === null) { tests.kill("SIGTERM"); await once(tests, "exit"); }
  await server?.close();
  if (fixture.exitCode === null) { fixture.kill("SIGINT"); await once(fixture, "exit"); }
  if (fixture.exitCode !== 0) { console.error(fixtureLog); process.exitCode = 1; }
  if (info) {
    try { await access(info.tokens); console.error("Ephemeral test tokens were not removed"); process.exitCode = 1; }
    catch { console.log("CLEANUP: temporary Console/Runtime/browser stopped; test token file removed"); }
  }
  process.off("SIGINT", interrupt);
  process.off("SIGTERM", interrupt);
}
