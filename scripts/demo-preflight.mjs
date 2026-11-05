import {access, stat} from "node:fs/promises";
import {constants} from "node:fs";
import {dirname, isAbsolute, join, resolve} from "node:path";
import {fileURLToPath} from "node:url";
import {createRequire} from "node:module";

export async function preflight({root, args, env, resolveModule}) {
  const modes = ["--f10-readonly", "--b3-read", "--f6-readonly", "--analytics-readonly", "--sales-readonly", "--ticket-readonly"];
  const allowed = new Set([...modes, "--verify-only", "--preflight-only"]);
  const lifetimeArgs = args.filter(arg => arg.startsWith("--lifetime-seconds="));
  const ordinaryArgs = args.filter(arg => !arg.startsWith("--lifetime-seconds="));
  if (lifetimeArgs.length > 1 || (lifetimeArgs.length && !args.includes("--f10-readonly"))) throw new Error("DEMO_CONFIG: lifetime-seconds仅供F10单一profile使用，不能重复。");
  const lifetime = lifetimeArgs[0]?.slice("--lifetime-seconds=".length) ?? "1680";
  if (!/^[1-9][0-9]*$/.test(lifetime) || Number(lifetime) < 60 || Number(lifetime) > 1680) throw new Error("DEMO_CONFIG: F10有效期必须为60–1680整数秒。");
  if (ordinaryArgs.some(arg => !allowed.has(arg))) throw new Error("DEMO_CONFIG: 未识别参数；使用--help查看支持的启动模式。");
  if (modes.filter(mode => args.includes(mode)).length > 1) throw new Error("DEMO_CONFIG: 一次只能选择一种场景模式。");
  const consoleRequire = createRequire(join(root, "apps/console-web/package.json"));
  const browserRequire = createRequire(join(root, "apps/console-web/tests/browser/support.mjs"));
  const resolveDependency = resolveModule ?? ((name, browser) => (browser ? browserRequire : consoleRequire).resolve(name));
  async function file(path, label, executable = false) {
    try {if (!(await stat(path)).isFile()) throw new Error(); await access(path, executable ? constants.R_OK | constants.X_OK : constants.R_OK);}
    catch {throw new Error(`DEMO_CONFIG: ${label}缺失或不可读取${executable ? "／执行" : ""}；请核对已有本机依赖，不会自动安装。`);}
    return path;
  }
  const python = await file(join(root, ".venv/bin/python"), "项目Python解释器", true);
  await file(join(root, "scripts/verify_s12_workbench.py"), "隔离夹具脚本");
  const modules = {};
  for (const name of ["vite", "@vitejs/plugin-vue"]) {
    let target;try {target = resolveDependency(name, false);} catch {throw new Error(`DEMO_CONFIG: 缺少${name}；请核对已有Console依赖，不会自动安装。`);}
    modules[name] = await file(target, name);
  }
  let playwright;
  try {
    const value = env.PLAYWRIGHT_MODULE;
    playwright = value?.startsWith("file:") ? fileURLToPath(value)
      : value && isAbsolute(value) ? value : resolveDependency(value || "playwright", true);
  } catch {throw new Error("DEMO_CONFIG: PLAYWRIGHT_MODULE无法解析；请指向已有本机Playwright入口，不会下载依赖或浏览器。");}
  await file(playwright, "PLAYWRIGHT_MODULE指定的本机入口");
  for (const page of ["scripts/local_demo.html", "scripts/local_demo_ticket_readonly.html", "apps/portal-web/demo.html"]) await file(join(root, page), page);
  if (args.includes("--f10-readonly")) await file(join(root, "scripts/local_demo_f10.html"), "F10体验入口");
  if (env.S12_DEMO_EVIDENCE) {
    let parent = resolve(env.S12_DEMO_EVIDENCE);
    while (true) {
      try {const info = await stat(parent);if (!info.isDirectory()) throw new Error("not directory");await access(parent, constants.W_OK);break;}
      catch (error) {
        if (error.code !== "ENOENT" || dirname(parent) === parent) throw new Error("DEMO_CONFIG: S12_DEMO_EVIDENCE必须指向可写目录；未创建临时环境。");
        parent = dirname(parent);
      }
    }
  }
  return {python, vite: modules.vite, vue: modules["@vitejs/plugin-vue"], playwright, lifetimeSeconds: Number(lifetime)};
}
