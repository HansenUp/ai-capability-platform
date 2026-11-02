/** Isolated host contract. No Tauri binding, process launch, network or persistent cache. */
export class IsolatedDesktopSession {
  #identity = null;
  #cache = new Map();
  #generation = 0;
  #handle = null;
  #lifecycleGeneration = 0;
  #now;
  #verify;
  #transport;
  #online = true;
  state = "STOPPED";

  constructor({ now, verifyIdentity, transport }) {
    this.#now = now;
    this.#verify = verifyIdentity;
    this.#transport = transport;
  }

  #clearIdentity() {
    this.#generation++;
    this.#identity = null;
    this.#cache.clear();
  }

  #currentIdentity() {
    const identity = this.#identity;
    if (!identity || !Number.isFinite(this.#now()) || this.#now() >= identity.expiresAt) {
      this.#clearIdentity();
      return null;
    }
    return identity;
  }

  async authenticate(credential) {
    // Clear before asynchronous verification: no previous subject remains visible.
    this.#clearIdentity();
    const generation = this.#generation;
    try {
      const identity = await this.#verify(credential);
      if (generation !== this.#generation) return false;
      if (!identity || ![identity.issuer, identity.tenant, identity.subject].every(x => typeof x === "string" && x.length > 0)
          || !Number.isFinite(identity.expiresAt) || identity.expiresAt <= this.#now()) throw new Error("identity unavailable");
      this.#identity = Object.freeze({ issuer: identity.issuer, tenant: identity.tenant, subject: identity.subject, expiresAt: identity.expiresAt });
      return true;
    } catch {
      if (generation === this.#generation) this.#clearIdentity();
      return false;
    }
  }

  logout() { this.#clearIdentity(); }
  setOnline(online) {
    if (typeof online !== "boolean") throw new Error("invalid connection state");
    if (online !== this.#online) this.#generation++; // discard in-flight reads across disconnect
    this.#online = online;
  }

  async start() {
    if (!["STOPPED", "FAILED"].includes(this.state)) throw new Error("sidecar already active");
    const generation = ++this.#lifecycleGeneration;
    this.state = "STARTING";
    try {
      const handle = await this.#transport.start();
      if (generation !== this.#lifecycleGeneration || this.state !== "STARTING") {
        try {
          await this.#transport.stop(handle); // only this start's owned handle
          this.state = "STOPPED";
        } catch {
          this.#handle = handle;
          this.state = "CLEANUP_UNKNOWN";
        }
        return false;
      }
      this.#handle = handle;
      this.state = "READY";
      return true;
    } catch {
      if (generation === this.#lifecycleGeneration) this.state = "FAILED";
      else if (this.state === "STOPPING") this.state = "STOPPED";
      return false; // no automatic restart
    }
  }

  async stop() {
    if (this.state === "STOPPING") throw new Error("cleanup already active");
    const starting = this.state === "STARTING";
    this.#lifecycleGeneration++;
    this.#clearIdentity();
    const handle = this.#handle;
    this.#handle = null;
    this.state = "STOPPING";
    if (starting) return; // pending start owns cleanup; block new starts until resolved
    try {
      if (handle !== null) await this.#transport.stop(handle);
      this.state = "STOPPED";
    } catch {
      this.state = "CLEANUP_UNKNOWN";
      this.#handle = handle;
      throw new Error("owned sidecar cleanup unconfirmed");
    }
  }

  async read(id) {
    const identity = this.#currentIdentity();
    if (!identity) throw new Error("authentication required");
    if (!this.#online) return this.cached(id);
    if (this.state !== "READY") throw new Error("sidecar unavailable");
    const generation = this.#generation;
    // Transport must perform authorization; browser input cannot assert authorization.
    const result = await this.#transport.read(this.#handle, id, identity);
    if (generation !== this.#generation || this.#currentIdentity() !== identity || !this.#online) return null;
    if (!result || result.synthetic !== true || result.id !== id || result.issuer !== identity.issuer
        || result.tenant !== identity.tenant || result.subject !== identity.subject
        || typeof result.text !== "string") throw new Error("authorized synthetic cache scope mismatch");
    const projected = Object.freeze({ id, text: result.text, synthetic: true });
    this.#cache.set(id, projected);
    return { ...projected };
  }

  cached(id) {
    if (!this.#currentIdentity()) return null;
    const result = this.#cache.get(id);
    return result ? { ...result } : null;
  }

  async write() {
    // This first isolated host has no business write bridge, even when online.
    throw new Error("business writes unavailable in isolated desktop host");
  }
}
