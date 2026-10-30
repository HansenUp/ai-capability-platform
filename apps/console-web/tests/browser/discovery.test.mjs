import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { browser, profile } from "./support.mjs";

test("E1a发现：实际浏览器GET、签名直接用户白名单、租户隔离且旧目录不变", async () => {
  const tokens = JSON.parse(await readFile(profile.tokens, "utf8"));
  const engine = await browser(), context = await engine.newContext(), page = await context.newPage();
  const writes = [];
  page.on("request", req => { if (req.url().includes("/v1/") && req.method() !== "GET") writes.push(req.method()); });
  try {
    await page.goto(profile.url);
    const read = async (token, path = "/v1/isolated-discovery/agents") => page.evaluate(async ({ token, path }) => {
      const response = await fetch(path, { headers: token ? { Authorization: `Bearer ${token}` } : {}, credentials: "omit", cache: "no-store", redirect: "error" });
      return { status: response.status, cache: response.headers.get("cache-control"), value: await response.json() };
    }, { token, path });
    const alice = await read(tokens.alice);
    assert.equal(alice.status, 200); assert.equal(alice.cache, "no-store");
    assert.deepEqual(alice.value, { profile: "isolated-e1a-v1", items: [{ agent_id: "s12-workbench", version: "1" }], snapshot_guaranteed: false });
    for (const token of [tokens.bob, tokens.foreign]) {
      const denied = await read(token); assert.equal(denied.status, 200); assert.deepEqual(denied.value.items, []);
    }
    assert.equal((await read()).status, 401);
    assert.equal((await read(tokens.expired_alice)).status, 401);
    const legacy = await read(tokens.bob, "/v1/agents");
    assert.equal(legacy.status, 200); assert.ok(legacy.value.some(row => row.agent_id === "s12-workbench"));
    assert.deepEqual(writes, []);
    assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
  } finally { await context.close(); await engine.close(); }
});
