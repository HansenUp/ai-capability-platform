import { before, after, test } from "node:test";
import assert from "node:assert/strict";
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const target = process.env.S12_OWNER_SESSION_URL;
if (!target || new URL(target).hostname !== "127.0.0.1") throw new Error("Isolated loopback fixture required");
let engine;
before(async () => { engine = await chromium.launch({ channel: process.env.S12_BROWSER_CHANNEL || "chrome", headless: true }); });
after(async () => { await engine?.close(); });
async function fixture(action) {
  const context = await engine.newContext(), page = await context.newPage(), external = [], errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await context.route("**/*", async route => {
    if (new URL(route.request().url()).origin !== new URL(target).origin) { external.push(route.request().url()); await route.abort(); }
    else await route.continue();
  });
  try {
    await page.goto(target + "/tests/browser/owner-session.html");
    await page.waitForFunction(() => !!window.ownerFixture);
    await action(page);
    assert.deepEqual(external, []);
    assert.deepEqual(errors, []);
    assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
  } finally { await context.close(); }
}
const call = (page, method, ...args) => page.evaluate(({ method, args }) => window.ownerFixture[method](...args), { method, args });
async function login(page, subject = "alice", tenant = "synthetic-owner") {
  await call(page, "select", subject, tenant); await call(page, "start");
  assert.equal(await call(page, "callback"), "ok");
}
const summary = page => page.getByRole("button", { name: "查询Owner摘要", exact: true });

test("Owner OIDC：S256／独立state和nonce、签名ID核验及内存身份展示", async () => fixture(async page => {
  await page.getByRole("button", { name: "发起Owner测试登录", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "等待替身回调" }).waitFor();
  const url = new URL(await call(page, "authorization")), params = url.searchParams;
  assert.equal(url.origin, "https://identity.test");
  assert.equal(params.get("response_type"), "code"); assert.equal(params.get("code_challenge_method"), "S256");
  assert.equal(params.get("scope"), "openid synthetic-owner-read"); assert.notEqual(params.get("state"), params.get("nonce"));
  assert.match(params.get("state"), /^[A-Za-z0-9_-]{43}$/);
  assert.equal(await call(page, "callback"), "ok");
  const { exchanges, verifications, requests } = await call(page, "calls");
  assert.equal(exchanges, 1); assert.equal(verifications, 1);
  const challenge = await page.evaluate(async verifier => {
    const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
    return btoa(String.fromCharCode(...digest)).replace(/=/g, "").replace(/\+/g, "-").replace(/\//g, "_");
  }, requests[0].verifier);
  assert.equal(challenge, params.get("code_challenge")); assert.match(requests[0].verifier, /^[A-Za-z0-9_-]{43}$/);
  await page.getByText("测试登录主体 alice", { exact: false }).waitFor();
  await page.getByLabel("Owner Agent ID", { exact: true }).fill("synthetic-agent");
  await summary(page).click(); await page.getByRole("heading", { name: "Owner摘要", exact: true }).waitFor();
}));

test("Owner OIDC：回调重放与并发消费不重复交换", async () => fixture(async page => {
  await login(page);
  assert.notEqual(await call(page, "callback"), "ok");
  assert.equal((await call(page, "calls")).exchanges, 1);
  await call(page, "mode", "exchange-held"); await call(page, "start");
  await page.evaluate(() => { window.pendingLogin = window.ownerFixture.callback(); });
  await page.waitForFunction(() => window.ownerFixture.pending());
  assert.notEqual(await call(page, "callback"), "ok");
  await call(page, "release");
  assert.equal(await page.evaluate(() => window.pendingLogin), "ok");
  assert.equal((await call(page, "calls")).exchanges, 2);
}));

for (const [name, change, duplicate] of [
  ["错误state", { state: "wrong" }, ""], ["issuer错配", { iss: "https://foreign.test" }, ""],
  ["重复code", {}, "code"], ["重复state", {}, "state"], ["错误回调字段", { error: "access_denied" }, ""],
]) test(`Owner OIDC：${name}拒收并消费事务`, async () => fixture(async page => {
  await call(page, "start"); assert.notEqual(await call(page, "callback", change, duplicate), "ok");
  assert.notEqual(await call(page, "callback"), "ok");
  assert.equal((await call(page, "calls")).exchanges, 0);
  assert.equal((await call(page, "snapshot")).token, "");
}));

for (const mode of ["nonce", "issuer", "audience", "id-expired", "signature", "lifetime", "refresh", "exchange-error"]) {
  test(`Owner OIDC：${mode}失败关闭且不回显上游正文`, async () => fixture(async page => {
    await call(page, "mode", mode); await call(page, "start");
    const message = await call(page, "callback");
    assert.notEqual(message, "ok"); assert.equal(message.includes("PRIVATE"), false);
    assert.equal((await call(page, "snapshot")).token, "");
    assert.equal((await call(page, "calls")).exchanges, 1);
  }));
}

test("Owner OIDC：300秒事务边界及600秒会话到期清空", async () => fixture(async page => {
  await call(page, "start"); await call(page, "advance", 300000);
  assert.notEqual(await call(page, "callback"), "ok"); assert.equal((await call(page, "calls")).exchanges, 0);
  await login(page); await call(page, "advance", 599999);
  assert.notEqual((await call(page, "snapshot")).token, "");
  await call(page, "advance", 1);
  assert.equal((await call(page, "snapshot")).token, "");
  assert.equal(await page.getByText("测试登录主体 alice", { exact: false }).count(), 0);
}));

test("Owner OIDC：后台计时器未触发时读前仍核对到期", async () => fixture(async page => {
  await login(page); await call(page, "advance", 600000, false);
  await page.getByLabel("Owner Agent ID", { exact: true }).fill("synthetic-agent");
  await summary(page).click();
  assert.equal((await call(page, "snapshot")).token, "");
  assert.equal(await call(page, "readStarted"), false);
}));

for (const mode of ["exchange-held", "verify-held"]) test(`Owner OIDC：${mode}旧登录不能覆盖新主体／租户`, async () => fixture(async page => {
  await call(page, "mode", mode); await call(page, "start");
  await page.evaluate(() => { window.pendingLogin = window.ownerFixture.callback(); });
  await page.waitForFunction(() => window.ownerFixture.pending());
  await call(page, "mode", "ok"); await login(page, "bob", "another-tenant");
  await call(page, "release"); assert.notEqual(await page.evaluate(() => window.pendingLogin), "ok");
  assert.deepEqual((await call(page, "snapshot")).identity, { issuer: "https://identity.test/realm", subject: "bob", tenant: "another-tenant" });
}));

test("Owner OIDC：重载丢弃登录态与未消费事务，不从浏览器存储恢复", async () => fixture(async page => {
  await login(page); await page.reload(); await page.waitForFunction(() => !!window.ownerFixture);
  assert.equal((await call(page, "snapshot")).token, "");
  const authorization = new URL(await call(page, "start")), callback = new URL(authorization.searchParams.get("redirect_uri"));
  callback.search = new URLSearchParams({ state: authorization.searchParams.get("state"), code: "old-code", iss: "https://identity.test/realm" }).toString();
  await page.reload(); await page.waitForFunction(() => !!window.ownerFixture);
  assert.equal(await call(page, "rawCallback", callback.toString()), "rejected");
  assert.equal((await call(page, "snapshot")).identity, null);
  assert.equal((await call(page, "calls")).exchanges, 0);
}));

test("Owner OIDC：换身份丢弃忽略abort的旧摘要响应", async () => fixture(async page => {
  await login(page); await page.getByLabel("Owner Agent ID", { exact: true }).fill("synthetic-agent");
  await call(page, "holdRead"); await summary(page).click();
  await page.waitForFunction(() => window.ownerFixture.readStarted());
  await login(page, "bob", "another-tenant"); await call(page, "releaseRead");
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  assert.equal(await page.getByRole("heading", { name: "Owner摘要", exact: true }).count(), 0);
  assert.equal((await call(page, "snapshot")).identity.subject, "bob");
}));

test("Owner OIDC：卸载时销毁会话，迟到回调不能复活", async () => fixture(async page => {
  await call(page, "mode", "exchange-held"); await call(page, "start");
  await page.evaluate(() => { window.pendingLogin = window.ownerFixture.callback(); });
  await page.waitForFunction(() => window.ownerFixture.pending());
  await call(page, "unmount"); await call(page, "release");
  assert.notEqual(await page.evaluate(() => window.pendingLogin), "ok");
  assert.equal((await call(page, "snapshot")).token, "");
}));

test("Owner OIDC：读取期间到期且计时器挂起，不接纳迟到摘要", async () => fixture(async page => {
  await login(page); await page.getByLabel("Owner Agent ID", { exact: true }).fill("synthetic-agent");
  await call(page, "holdRead"); await summary(page).click();
  await page.waitForFunction(() => window.ownerFixture.readStarted());
  await call(page, "advance", 600000, false); await call(page, "releaseRead");
  await page.waitForFunction(() => !window.ownerFixture.snapshot().token);
  assert.equal(await page.getByRole("heading", { name: "Owner摘要", exact: true }).count(), 0);
}));

test("Owner OIDC：独立scope与回调目标强约束，配置失败不交换", async () => fixture(async page => {
  assert.equal(await call(page, "invalidProfile"), "rejected");
  const url = new URL(await call(page, "start"));
  const callback = new URL(url.searchParams.get("redirect_uri")); callback.pathname = "/wrong-callback";
  callback.search = new URLSearchParams({ state: url.searchParams.get("state"), code: "code", iss: "https://identity.test/realm" }).toString();
  assert.equal(await call(page, "rawCallback", callback.toString()), "rejected");
  assert.equal((await call(page, "calls")).exchanges, 0);
}));

test("Owner OIDC：300秒前消费可用，交换未完成到期不可复活", async () => fixture(async page => {
  await call(page, "start"); await call(page, "advance", 299999);
  assert.equal(await call(page, "callback"), "ok");
  await call(page, "mode", "exchange-held"); await call(page, "start");
  await page.evaluate(() => { window.pendingLogin = window.ownerFixture.callback(); });
  await page.waitForFunction(() => window.ownerFixture.pending());
  await call(page, "advance", 300000); await call(page, "release");
  assert.notEqual(await page.evaluate(() => window.pendingLogin), "ok");
  assert.equal((await call(page, "snapshot")).token, "");
}));

test("Owner OIDC：未注入会话时保留原手动测试入口且不启用登录", async () => fixture(async page => {
  await call(page, "unmount"); await call(page, "remount");
  await page.getByLabel("Owner用户令牌", { exact: true }).fill("synthetic-manual");
  await page.getByRole("button", { name: "应用Owner身份并清空", exact: true }).click();
  assert.equal(await page.getByRole("button", { name: "发起Owner测试登录", exact: true }).count(), 0);
  assert.equal(await page.getByLabel("Owner用户令牌", { exact: true }).inputValue(), "");
}));

test("Owner OIDC：响应租户与核验主体不匹配时清空会话", async () => fixture(async page => {
  await login(page); await call(page, "mode", "response-tenant");
  await page.getByLabel("Owner Agent ID", { exact: true }).fill("synthetic-agent");
  await summary(page).click(); await page.getByRole("alert").filter({ hasText: "Owner响应租户不一致" }).waitFor();
  assert.equal((await call(page, "snapshot")).token, "");
  assert.equal(await page.getByRole("heading", { name: "Owner摘要", exact: true }).count(), 0);
}));
