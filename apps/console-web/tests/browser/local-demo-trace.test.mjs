import test from "node:test";
import assert from "node:assert/strict";
import { readFile, writeFile } from "node:fs/promises";
import { browser, profile } from "./support.mjs";

test("独立演示：同一已完成Run的只读轨迹页面与事件一致，不连接真实Langfuse", async () => {
  if (!process.env.S12_BROWSER_EVIDENCE) throw new Error("Demo evidence directory required");
  const resultPath = `${process.env.S12_BROWSER_EVIDENCE}/result.json`;
  const result = JSON.parse(await readFile(resultPath, "utf8")), tokens = JSON.parse(await readFile(profile.tokens, "utf8"));
  const engine = await browser(), context = await engine.newContext(), page = await context.newPage(), requests = [], external = [];
  await context.route("**/*", async route => {
    if (new URL(route.request().url()).origin !== new URL(profile.url).origin) { external.push(route.request().url()); await route.abort(); }
    else await route.continue();
  });
  page.on("request", request => { if (new URL(request.url()).pathname.startsWith("/v1/")) requests.push(request); });
  try {
    await page.goto(profile.url + "/manage/runs");
    await page.getByLabel("访问令牌", { exact: true }).fill(tokens.alice);
    await page.getByRole("button", { name: "应用读取身份并清空旧数据", exact: true }).click();
    await page.getByLabel("Run ID", { exact: true }).fill(result.run_id);
    await page.getByRole("button", { name: "读取指定 Run", exact: true }).click();
    await page.getByRole("heading", { name: `${result.run_id} · completed`, exact: true }).waitFor();
    await page.getByRole("button", { name: "增量刷新轨迹", exact: true }).waitFor({ state: "visible" });
    await page.waitForFunction(() => ![...document.querySelectorAll("button")].find(button => button.textContent === "增量刷新轨迹")?.disabled);
    const eventNames = await page.locator("section ol li > b").allTextContents();
    for (const name of ["run.created", "tool.approved", "tool.executed", "input.provided", "model.response"]) {
      assert.equal(eventNames.some(text => text.endsWith(name)), true, name);
    }
    assert.equal(requests.every(request => request.method() === "GET"), true);
    assert.deepEqual(external, []);
    assert.equal(await page.getByRole("link", { name: "在本机 Langfuse 查看 Trace", exact: true }).count(), 0);
    await page.screenshot({ path: `${process.env.S12_BROWSER_EVIDENCE}/trace.png`, fullPage: true });
    await writeFile(resultPath, JSON.stringify({ ...result, trace_ui_verified: true, trace_event_count: eventNames.length,
      trace_requests_read_only: true, real_langfuse_connected: false }, null, 2));
  } finally { await context.close(); await engine.close(); }
});
