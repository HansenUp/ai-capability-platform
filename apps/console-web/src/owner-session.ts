import type { InjectionKey } from "vue";

export interface OwnerIdentity { issuer: string; tenant: string; subject: string }
export interface OwnerSessionView { token: string; identity: OwnerIdentity | null; expiresAt: number }
export interface OwnerOidcProfile {
  issuer: string; client: string; redirectUri: string; authorizationEndpoint: string; ownerScope: string;
}
interface ExchangeRequest { code: string; verifier: string; client: string; redirectUri: string; issuer: string }
export interface OwnerSessionDependencies {
  exchange(request: ExchangeRequest): Promise<unknown>;
  verifyIdToken(token: string): Promise<unknown>;
  clock?: () => number;
  schedule?: (callback: () => void, milliseconds: number) => () => void;
}
interface Transaction { state: string; nonce: string; verifier: string; started: number; generation: number }
export interface OwnerSessionConnection { session: OwnerSession; authorize(url: string): void }
export const ownerSessionKey: InjectionKey<OwnerSessionConnection> = Symbol("isolated-owner-session");

function base64url(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes)).replace(/=/g, "").replace(/\+/g, "-").replace(/\//g, "_");
}
function random(): string { return base64url(crypto.getRandomValues(new Uint8Array(32))); }
function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Owner登录响应无效。");
  return value as Record<string, unknown>;
}
function text(value: unknown, maximum = 256): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= maximum && !/[\s\x00-\x1f\x7f]/.test(value);
}

export class OwnerSession {
  private readonly profile: Readonly<OwnerOidcProfile>;
  private readonly dependencies: OwnerSessionDependencies;
  private readonly clock: () => number;
  private readonly schedule: (callback: () => void, milliseconds: number) => () => void;
  private transaction: Transaction | null = null;
  private generation = 0;
  private closed = false;
  private value: OwnerSessionView = { token: "", identity: null, expiresAt: 0 };
  private cancelTimer: (() => void) | null = null;
  private listeners = new Set<(view: OwnerSessionView) => void>();

  constructor(profile: OwnerOidcProfile, dependencies: OwnerSessionDependencies) {
    if ([profile.issuer, profile.authorizationEndpoint, profile.redirectUri].some(value => !text(value, 2048))) throw new Error("Owner登录配置无效。");
    const issuer = new URL(profile.issuer), endpoint = new URL(profile.authorizationEndpoint), redirect = new URL(profile.redirectUri);
    if (issuer.protocol !== "https:" || endpoint.protocol !== "https:" || endpoint.origin !== issuer.origin
      || [issuer, endpoint, redirect].some(url => url.username || url.password || url.search || url.hash)
      || !text(profile.client) || !text(profile.ownerScope) || ["openid", "offline_access", "act", "ai-runtime"].includes(profile.ownerScope)
      || !(redirect.protocol === "https:"
        || (redirect.protocol === "http:" && redirect.hostname === "127.0.0.1"))) throw new Error("Owner登录配置无效。");
    this.profile = Object.freeze({ ...profile }); this.dependencies = dependencies;
    this.clock = dependencies.clock ?? Date.now;
    this.schedule = dependencies.schedule ?? ((callback, milliseconds) => { const timer = setTimeout(callback, milliseconds); return () => clearTimeout(timer); });
  }

  private notify(): void { for (const listener of this.listeners) listener(this.snapshot()); }
  private reset(): void {
    this.generation += 1; this.transaction = null; this.cancelTimer?.(); this.cancelTimer = null;
    this.value = { token: "", identity: null, expiresAt: 0 };
  }
  snapshot(): OwnerSessionView {
    if (this.value.token && this.clock() >= this.value.expiresAt) { this.reset(); this.notify(); }
    return { ...this.value, identity: this.value.identity ? { ...this.value.identity } : null };
  }
  subscribe(listener: (view: OwnerSessionView) => void): () => void {
    this.listeners.add(listener); listener(this.snapshot()); return () => this.listeners.delete(listener);
  }
  logout(): void { this.reset(); this.notify(); }
  dispose(): void { this.closed = true; this.logout(); this.listeners.clear(); }

  async begin(): Promise<string> {
    if (this.closed) throw new Error("Owner会话已卸载。");
    this.logout();
    const generation = this.generation, started = this.clock(), verifier = random(), state = random(), nonce = random();
    const challenge = base64url(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier))));
    if (this.closed || generation !== this.generation || this.clock() - started >= 300000) throw new Error("Owner登录事务已失效。");
    this.transaction = { generation, started, verifier, state, nonce };
    this.cancelTimer = this.schedule(() => { if (generation === this.generation) { this.reset(); this.notify(); } }, 300000 - (this.clock() - started));
    const url = new URL(this.profile.authorizationEndpoint);
    url.search = new URLSearchParams({ response_type: "code", client_id: this.profile.client, redirect_uri: this.profile.redirectUri,
      scope: `openid ${this.profile.ownerScope}`, state, nonce, code_challenge: challenge, code_challenge_method: "S256" }).toString();
    return url.toString();
  }

  async callback(callbackUrl: string): Promise<void> {
    const transaction = this.transaction; this.transaction = null;
    if (this.closed || !transaction) throw new Error("Owner登录事务不存在或已消费。");
    const current = () => !this.closed && transaction.generation === this.generation;
    try {
      const url = new URL(callbackUrl), expected = new URL(this.profile.redirectUri), params = url.searchParams;
      if (!current() || callbackUrl.length > 8192 || /[\s\x00-\x1f\x7f]/.test(callbackUrl) || url.username || url.password
        || url.origin !== expected.origin || url.pathname !== expected.pathname || url.hash
        || this.clock() < transaction.started || this.clock() - transaction.started >= 300000
        || [...params.keys()].some(name => !["code", "state", "iss"].includes(name))
        || ["code", "state", "iss"].some(name => params.getAll(name).length !== 1)
        || params.get("state") !== transaction.state || params.get("iss") !== this.profile.issuer || !text(params.get("code"), 2048)) {
        throw new Error("Owner登录回调无效或已过期。");
      }
      const response = record(await this.dependencies.exchange({ code: params.get("code")!, verifier: transaction.verifier,
        client: this.profile.client, redirectUri: this.profile.redirectUri, issuer: this.profile.issuer }));
      const receivedAt = this.clock();
      if (!current()) throw new Error("Owner登录身份已切换。");
      if (Object.keys(response).some(name => !["access_token", "id_token", "token_type", "expires_in"].includes(name))
        || response.token_type !== "Bearer" || !text(response.access_token, 16384) || !text(response.id_token, 16384)
        || !Number.isSafeInteger(response.expires_in) || (response.expires_in as number) <= 0 || (response.expires_in as number) > 600) {
        throw new Error("Owner登录令牌响应无效。");
      }
      const claims = record(await this.dependencies.verifyIdToken(response.id_token as string)), stamp = this.clock() / 1000;
      if (!current()) throw new Error("Owner登录身份已切换。");
      if (this.clock() < transaction.started || this.clock() - transaction.started >= 300000
        || claims.iss !== this.profile.issuer || claims.aud !== this.profile.client || claims.azp !== this.profile.client
        || claims.nonce !== transaction.nonce || !text(claims.sub) || !text(claims.tenant)
        || ["exp", "iat", "nbf"].some(name => !Number.isSafeInteger(claims[name]))
        || (claims.exp as number) <= stamp || (claims.iat as number) > stamp || (claims.nbf as number) > stamp
        || (claims.exp as number) <= (claims.iat as number) || (claims.exp as number) - (claims.iat as number) > 600) {
        throw new Error("Owner登录身份核验失败。");
      }
      const expiresAt = Math.min(receivedAt + (response.expires_in as number) * 1000, (claims.exp as number) * 1000);
      if (this.clock() >= expiresAt) throw new Error("Owner登录令牌已过期。");
      this.value = { token: response.access_token as string, identity: { issuer: claims.iss as string,
        tenant: claims.tenant as string, subject: claims.sub as string }, expiresAt };
      this.cancelTimer?.();
      this.cancelTimer = this.schedule(() => { if (current()) { this.reset(); this.notify(); } }, expiresAt - this.clock());
      this.notify();
    } catch {
      if (current()) { this.reset(); this.notify(); }
      throw new Error("Owner登录未完成，请重新发起登录；不会自动重试。");
    }
  }
}
