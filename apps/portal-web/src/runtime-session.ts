import type { InjectionKey } from "vue";

/** Explicit host token provider; absence uses the existing Portal OIDC session. */
export const portalTokenKey: InjectionKey<() => Promise<string>> = Symbol("portal-runtime-token");
