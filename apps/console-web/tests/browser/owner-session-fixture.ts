import { createApp } from "vue";
import OwnerOperations from "../../src/views/OwnerOperations.vue";
import { OwnerSession, ownerSessionKey } from "../../src/owner-session";

const encoder = new TextEncoder();
const base64 = (bytes: Uint8Array) => btoa(String.fromCharCode(...bytes)).replace(/=/g, "").replace(/\+/g, "-").replace(/\//g, "_");
const decode = (value: string) => Uint8Array.from(atob(value.replace(/-/g, "+").replace(/_/g, "/")), item => item.charCodeAt(0));
const keys = await crypto.subtle.generateKey({ name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" }, false, ["sign", "verify"]);
let time = 1800000000000, subject = "alice", tenant = "synthetic-owner", auth = "", held: (() => void) | null = null;
let mode = "ok", exchanges = 0, verifications = 0;
const timers = new Set<{ at: number; callback: () => void }>();
const calls: { code: string; verifier: string; client: string; redirectUri: string; issuer: string }[] = [];
const profile = { issuer: "https://identity.test/realm", client: "synthetic-owner-client",
  redirectUri: `${location.origin}/tests/browser/owner-session.html`, authorizationEndpoint: "https://identity.test/authorize", ownerScope: "synthetic-owner-read" };
const wait = () => new Promise<void>(resolve => { held = resolve; });
async function sign(claims: object) {
  const header = base64(encoder.encode(JSON.stringify({ alg: "RS256", typ: "JWT", kid: "synthetic-id" })));
  const payload = base64(encoder.encode(JSON.stringify(claims))), input = `${header}.${payload}`;
  return `${input}.${base64(new Uint8Array(await crypto.subtle.sign("RSASSA-PKCS1-v1_5", keys.privateKey, encoder.encode(input))))}`;
}
const session = new OwnerSession(profile, {
  clock: () => time,
  schedule(callback, milliseconds) { const timer = { at: time + milliseconds, callback }; timers.add(timer); return () => { timers.delete(timer); }; },
  async exchange(request) {
    exchanges += 1; calls.push(request);
    const selectedMode = mode, selectedSubject = subject, selectedTenant = tenant, nonce = new URL(auth).searchParams.get("nonce");
    if (selectedMode === "exchange-held") await wait();
    if (selectedMode === "exchange-error") throw new Error("PRIVATE-TOKEN-ENDPOINT-BODY");
    const stamp = Math.floor(time / 1000);
    const claims: Record<string, unknown> = { iss: profile.issuer, aud: profile.client, azp: profile.client, nonce,
      sub: selectedSubject, tenant: selectedTenant, iat: stamp, nbf: stamp, exp: stamp + 600 };
    if (selectedMode === "nonce") claims.nonce = "wrong";
    if (selectedMode === "issuer") claims.iss = "https://foreign.test";
    if (selectedMode === "audience") claims.aud = "ai-runtime";
    if (selectedMode === "id-expired") claims.exp = stamp;
    let id = await sign(claims);
    if (selectedMode === "signature") id = id.slice(0, -20) + "AAAAAAAAAAAAAAAAAAAA";
    return { access_token: `synthetic-access-${selectedTenant}-${selectedSubject}`, id_token: id,
      token_type: "Bearer", expires_in: selectedMode === "lifetime" ? 601 : 600,
      ...(selectedMode === "refresh" ? { refresh_token: "not-supported" } : {}) };
  },
  async verifyIdToken(token) {
    verifications += 1;
    if (mode === "verify-held") await wait();
    const [header, payload, signature] = token.split(".");
    const parsed = JSON.parse(new TextDecoder().decode(decode(header)));
    if (parsed.alg !== "RS256" || parsed.typ !== "JWT" || parsed.kid !== "synthetic-id"
      || !await crypto.subtle.verify("RSASSA-PKCS1-v1_5", keys.publicKey, decode(signature), encoder.encode(`${header}.${payload}`))) throw new Error("signature rejected");
    return JSON.parse(new TextDecoder().decode(decode(payload)));
  },
});
let app = createApp(OwnerOperations);
app.provide(ownerSessionKey, { session, authorize(url) { auth = url; } }); app.mount("#app");
const nativeFetch = window.fetch;
let heldRead: (() => void) | null = null, holdRead = false, readStarted = false;
window.fetch = async (input, init) => {
  if (String(input).startsWith("/console/owner/")) {
    const selectedTenant = session.snapshot().identity?.tenant;
    readStarted = true;
    if (holdRead) await new Promise<void>(resolve => { heldRead = resolve; });
    return new Response(JSON.stringify({ scope: "owner_agent_summary_v1", schema_version: 1, agent_id: "synthetic-agent",
      tenant_id: mode === "response-tenant" ? "foreign" : selectedTenant, snapshot_guaranteed: false, authorization_policy: "current_assignment_rechecked_v1",
      read_finished_at: new Date(time).toISOString(), versions: { draft: 1, published: 2, retired: 0 } }), { headers: { "Content-Type": "application/json" } });
  }
  return nativeFetch(input, init);
};
Object.assign(window, { ownerFixture: {
  snapshot: () => session.snapshot(),
  authorization: () => auth,
  calls: () => ({ exchanges, verifications, requests: calls }),
  select(nextSubject: string, nextTenant = "synthetic-owner") { subject = nextSubject; tenant = nextTenant; },
  mode(value: string) { mode = value; },
  advance(milliseconds: number, fire = true) { time += milliseconds; if (fire) for (const timer of [...timers]) if (timer.at <= time) { timers.delete(timer); timer.callback(); } },
  async start() { auth = await session.begin(); return auth; },
  async callback(change: Record<string, string> = {}, duplicate = "") {
    const url = new URL(profile.redirectUri), params = new URL(auth).searchParams;
    url.search = new URLSearchParams({ state: params.get("state")!, code: "synthetic-code", iss: profile.issuer, ...change }).toString();
    if (duplicate) url.searchParams.append(duplicate, "duplicate");
    try { await session.callback(url.toString()); return "ok"; } catch (error) { return (error as Error).message; }
  },
  async rawCallback(url: string) { try { await session.callback(url); return "ok"; } catch { return "rejected"; } },
  pending: () => !!held, release() { const resume = held; held = null; resume?.(); },
  holdRead() { holdRead = true; }, readStarted: () => readStarted, releaseRead() { heldRead?.(); },
  logout: () => session.logout(), unmount() { app.unmount(); },
  invalidProfile() { try { new OwnerSession({ ...profile, ownerScope: "act" }, { exchange: async () => ({}), verifyIdToken: async () => ({}) }); return "accepted"; } catch { return "rejected"; } },
  remount() { app = createApp(OwnerOperations); app.mount("#app"); },
} });
