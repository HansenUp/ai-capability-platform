/**
 * 门户登录（S3.4）：Keycloak OIDC 授权码 + PKCE（S256），公开客户端 portal-web，不引入第三方库。
 *
 * 流程：login() 生成 code_verifier / state 存入 sessionStorage 后跳转 Keycloak；回到 /callback 时
 * handleCallback() 校验 state、用 code + code_verifier 换取令牌；getToken() 在令牌临近过期时用 refresh_token 续期。
 * 令牌只存 sessionStorage（随标签页关闭失效），不写 localStorage。
 *
 * 配置（Vite 环境变量，缺省为本机 compose 的 Keycloak）：VITE_OIDC_ISSUER、VITE_OIDC_CLIENT_ID。
 */

const ISSUER = import.meta.env.VITE_OIDC_ISSUER ?? "http://localhost:8180/realms/ai-platform";
const CLIENT_ID = import.meta.env.VITE_OIDC_CLIENT_ID ?? "portal-web";
const REDIRECT_URI = `${window.location.origin}/callback`;
const STORE = "portal.oidc"; // sessionStorage 键：令牌
const PENDING = "portal.oidc.pending"; // sessionStorage 键：登录进行中的 verifier / state / 回跳地址

interface Tokens { access_token: string; refresh_token?: string; id_token?: string; expires_at: number }
interface Pending { verifier: string; state: string; returnTo: string }

/** 32 字节随机数的 base64url 编码。 */
function randomString(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return base64url(bytes);
}

function base64url(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function challenge(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64url(new Uint8Array(digest));
}

function load(): Tokens | null {
  const raw = sessionStorage.getItem(STORE);
  return raw ? (JSON.parse(raw) as Tokens) : null;
}

function save(body: { access_token: string; refresh_token?: string; id_token?: string; expires_in: number }): Tokens {
  const t: Tokens = { access_token: body.access_token, refresh_token: body.refresh_token, id_token: body.id_token, expires_at: Date.now() + body.expires_in * 1000 };
  sessionStorage.setItem(STORE, JSON.stringify(t));
  return t;
}

async function tokenRequest(params: Record<string, string>): Promise<Tokens> {
  const res = await fetch(`${ISSUER}/protocol/openid-connect/token`, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ client_id: CLIENT_ID, ...params }),
  });
  if (!res.ok) throw new Error(`token endpoint returned HTTP ${res.status}`);
  return save(await res.json());
}

/** 跳转到 Keycloak 登录；登录后回到 returnTo（默认当前页）。 */
export async function login(returnTo: string = window.location.pathname + window.location.search): Promise<void> {
  const pending: Pending = { verifier: randomString(), state: randomString(), returnTo };
  sessionStorage.setItem(PENDING, JSON.stringify(pending));
  const q = new URLSearchParams({
    response_type: "code",
    client_id: CLIENT_ID,
    redirect_uri: REDIRECT_URI,
    scope: "openid",
    state: pending.state,
    code_challenge: await challenge(pending.verifier),
    code_challenge_method: "S256",
  });
  window.location.assign(`${ISSUER}/protocol/openid-connect/auth?${q}`);
}

/** 处理 /callback：校验 state，换取令牌，返回登录前的页面地址。 */
export async function handleCallback(search: string): Promise<string> {
  const params = new URLSearchParams(search);
  const raw = sessionStorage.getItem(PENDING);
  sessionStorage.removeItem(PENDING);
  if (!raw) throw new Error("no login in progress");
  const pending = JSON.parse(raw) as Pending;
  if (params.get("error")) throw new Error(`login failed: ${params.get("error")}`);
  if (params.get("state") !== pending.state) throw new Error("state mismatch");
  await tokenRequest({ grant_type: "authorization_code", code: params.get("code") ?? "", redirect_uri: REDIRECT_URI, code_verifier: pending.verifier });
  return pending.returnTo || "/";
}

/** 登录已失效：清空本地令牌并跳转重新登录（登录后回到当前页面），同时抛错中止当前调用。 */
async function relogin(reason: string): Promise<never> {
  sessionStorage.removeItem(STORE);
  await login();
  throw new Error(reason);
}

/** 当前访问令牌；剩余有效期不足 30 秒时用 refresh_token 续期；没有令牌或续期失败时自动跳转重新登录。 */
export async function getToken(): Promise<string> {
  const t = load();
  if (!t) return relogin("not logged in");
  if (t.expires_at - Date.now() > 30_000) return t.access_token;
  if (!t.refresh_token) return relogin("session expired");
  try {
    return (await tokenRequest({ grant_type: "refresh_token", refresh_token: t.refresh_token })).access_token;
  } catch {
    return relogin("session expired");
  }
}

export function isLoggedIn(): boolean {
  return load() !== null;
}

/** 访问令牌中的展示用身份（只解码不验签，验签由 Runtime 完成）。 */
export function currentUser(): { name: string; tenant: string } | null {
  const t = load();
  if (!t) return null;
  const payload = JSON.parse(atob(t.access_token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
  return { name: payload.preferred_username ?? payload.sub, tenant: payload.tenant ?? "" };
}

/** 退出：清空本地令牌并结束 Keycloak 会话。 */
export function logout(): void {
  const t = load();
  sessionStorage.removeItem(STORE);
  const q = new URLSearchParams({ client_id: CLIENT_ID, post_logout_redirect_uri: `${window.location.origin}/` });
  if (t?.id_token) q.set("id_token_hint", t.id_token);
  window.location.assign(`${ISSUER}/protocol/openid-connect/logout?${q}`);
}
