// UI cache lifetime only. Signature, issuer, audience and permission checks remain
// server responsibilities; parsing these claims never authorizes a request.
export function testTokenExpiry(token: string): number | null {
  const parts = token.split('.');
  if (parts.length !== 3 || parts.some(part => !/^[A-Za-z0-9_-]+$/.test(part))) return null;
  try {
    const body = parts[1]!.replace(/-/g, '+').replace(/_/g, '/');
    const bytes = Uint8Array.from(atob(body.padEnd(Math.ceil(body.length / 4) * 4, '=')), c => c.charCodeAt(0));
    const claims: unknown = JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(bytes));
    if (!claims || typeof claims !== 'object' || Array.isArray(claims) || !('exp' in claims)) return null;
    const expiry = claims.exp;
    return typeof expiry === 'number' && Number.isSafeInteger(expiry) && expiry > 0 && Number.isSafeInteger(expiry * 1000)
      ? expiry * 1000 : null;
  } catch { return null; }
}
