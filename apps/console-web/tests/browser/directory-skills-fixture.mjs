import assert from "node:assert/strict";
import { profile } from "./support.mjs";
export const user = { id: 1, tenant_id: "default", issuer: "https://idp.test/realm", subject: "synthetic-subject", display_name: "合成用户", description: "synthetic directory metadata" };
export const skill = { name: "synthetic-skill", version: "1", tenant: "default", status: "evaluated", submitted_by: "human:author", reviewed_by: null,
  manifest_sha256: "a".repeat(64), scan_report: { passed: true, rules_version: "synthetic-rules", findings: [{ severity: "info", rule_id: "SYNTHETIC", file: "safe.md", line: 2, message: "synthetic finding" }] },
  eval: { run_ref: "synthetic-eval", pass_rate: null, items: [] } };
export const token = "synthetic-reviewer-token";
export async function mountDirectorySkills(engine, view, action) {
  const context = await engine.newContext();
  await context.addInitScript(() => {
    localStorage.setItem("console.operator", JSON.stringify({ user: "ops", role: "TENANT_ADMIN", tenant: "default" }));
    const native = window.fetch;
    window.syntheticTransport = { hold: false, pending: [], paths: [] };
    window.fetch = async (input, options) => {
      const path = new URL(String(input), location.href).pathname;
      if (!path.startsWith("/console/")) return native(input, options);
      const state = window.syntheticTransport, held = state.hold;
      state.paths.push(path);
      const response = await native(input, { ...options, signal: undefined });
      if (!held) return response;
      const text = await response.text();
      let release;
      const barrier = new Promise(resolve => { release = resolve; });
      state.pending.push(release);
      return { ok: response.ok, status: response.status, text: async () => { await barrier; return text; },
        json: async () => { await barrier; return JSON.parse(text); } };
    };
  });
  const page = await context.newPage(); page.setDefaultTimeout(5000);
  const calls = [], errors = [];
  page.on("pageerror", error => errors.push(error.message));
  let handler = async () => false;
  await context.route("**/console/**", async route => {
    const request = route.request(), url = new URL(request.url());
    const row = { path: url.pathname.replace(/^\/console/, ""), url, method: request.method(), headers: request.headers(), body: request.postDataJSON() };
    calls.push(row);
    if (await handler(route, row)) return;
    if (row.method === "GET") {
      if (row.path === "/directory/tenant") return route.fulfill({ json: { tenant_id: "default", scope: "display_metadata_only", metadata: { display_name: "合成租户", description: "synthetic tenant metadata" } } });
      if (row.path.startsWith("/directory/users")) return route.fulfill({ json: { items: [user], next_after_id: null, scope: "display_metadata_only" } });
      if (row.path === "/skills") return route.fulfill({ json: [skill] });
      if (row.path === "/skills/synthetic-skill/versions/1") return route.fulfill({ json: skill });
    }
    return route.fulfill({ status: 404, json: { detail: "Unconfigured isolated component request" } });
  });
  try {
    await page.goto(`${profile.url}/tests/browser/management.html?view=${view}`);
    await page.getByRole("row").filter({ hasText: view === "directory" ? user.subject : `${skill.name}@${skill.version}` }).waitFor();
    await action({ page, calls, handle: value => { handler = value; }, writes: () => calls.filter(row => row.method !== "GET") });
    assert.deepEqual(errors, [], "Component must handle request failures without unhandled page errors");
    assert.equal(await page.evaluate(value => JSON.stringify(localStorage).includes(value) || JSON.stringify(sessionStorage).includes(value), token), false);
  } finally { await context.close(); }
}
export const skillRow = page => page.getByRole("row").filter({ hasText: `${skill.name}@${skill.version}` });
export const tokenField = page => page.getByPlaceholder("skill-reviewer 用户访问令牌（X-AI-User-Token，仅内存）", { exact: true });
export const skillIdentity = page => page.getByRole("group", { name: "当前操作者（开发态身份头）", exact: true });
export async function reviewer(page) {
  await tokenField(page).fill(token);
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await skillRow(page).waitFor();
}
export async function directoryIdentity(page, tenant, role = "TENANT_ADMIN") {
  await page.getByLabel("租户", { exact: true }).fill(tenant);
  await page.getByRole("combobox", { name: /^角色/ }).selectOption(role);
  await page.getByRole("button", { name: "应用开发身份", exact: true }).click();
}
export async function dialog(page, button, answer) {
  page.once("dialog", async prompt => { if (answer === null) await prompt.dismiss(); else await prompt.accept(answer); });
  await button.click();
}
