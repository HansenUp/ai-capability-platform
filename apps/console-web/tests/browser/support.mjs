import { ownedBrowser } from "./owned-browser.mjs";
import assert from "node:assert/strict";
import { setTimeout } from "node:timers/promises";
export const profile = {
  url: process.env.S12_BROWSER_URL,
  runtime: process.env.S12_RUNTIME_URL,
  tokens: process.env.S12_TEST_TOKEN_FILE,
};
if (!profile.url || !profile.runtime || !profile.tokens) throw new Error("Run through scripts/verify_s12_browser.mjs with isolated fixture metadata.");
for (const value of [profile.url, profile.runtime]) if (new URL(value).hostname !== "127.0.0.1") throw new Error("Only isolated loopback test targets allowed.");
const playwright = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
let diagnosticId = 0;
async function lifecycle(label, action) {
  const id = ++diagnosticId, started = performance.now();
  console.log(`BROWSER_PHASE ${id} ${label} start`);
  try { return await action(); }
  finally { console.log(`BROWSER_PHASE ${id} ${label} end ms=${Math.round(performance.now() - started)}`); }
}
function instrument(target, methods) {
  for (const [method, wrap] of Object.entries(methods)) {
    const original = target[method].bind(target);
    target[method] = (...args) => lifecycle(method, async () => {
      const result = await original(...args);
      return wrap ? wrap(result) : result;
    });
  }
  return target;
}
const instrumentPage = page => instrument(page, { close: null });
const instrumentContext = context => instrument(context, { newPage: instrumentPage, close: null });
export async function browser() {
  const engine = await lifecycle("launch", () => ownedBrowser(playwright.chromium, { channel: process.env.S12_BROWSER_CHANNEL || "chrome", headless: true }));
  return instrument(engine, { newContext: instrumentContext, newPage: instrumentPage, close: null });
}
export async function apply(page, token) {
  await page.getByLabel("用户访问令牌", { exact: true }).fill(token);
  await page.getByRole("button", { name: "应用身份并清空旧数据", exact: true }).click();
  await page.getByText("当前已应用：", { exact: false }).waitFor();
  await page.getByRole("button", { name: "只读核对写账本", exact: true }).waitFor();
  await page.waitForFunction(() => ![...document.querySelectorAll("button")].find(button => button.textContent === "只读核对写账本")?.disabled);
  assert.equal(await page.getByLabel("用户访问令牌", { exact: true }).inputValue(), "");
}
export const ledgerIdentity = { protocol: "workbench-v1", auth_method: "user", scope_id: "a".repeat(64), tenant_id: "synthetic", subject: "human:alice" };
export async function mockLedgerRead(route) {
  const path = new URL(route.request().url()).pathname;
  if (route.request().method() !== "GET") return false;
  if (path === "/v1/workbench/identity") { await route.fulfill({ json: ledgerIdentity }); return true; }
  if (path === "/v1/workbench/attempts") { await route.fulfill({ json: { scope_id: ledgerIdentity.scope_id, active: null } }); return true; }
  return false;
}
export async function phase(page, runId, expected, token) {
  const deadline = Date.now() + 10000;
  let state;
  while (Date.now() < deadline) {
    const response = await page.request.get(`${profile.runtime}/v1/runs/${runId}`, { headers: { Authorization: `Bearer ${token}` } });
    assert.equal(response.status(), 200);
    state = (await response.json()).state;
    if (state === expected) break;
    await setTimeout(25);
  }
  assert.equal(state, expected);
  await page.getByRole("button", { name: "读取 Run 与待办卡片", exact: true }).click();
  await page.getByRole("heading", { name: new RegExp(`${runId} · ${expected}`) }).waitFor();
}
