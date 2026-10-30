/** Executable contract model ONLY: injected in-memory driver, no OS/Tauri calls. */
export class NativeBridgeModel {
  #driver; #clear; #handle = null; #closing = false;
  state = "STOPPED";
  constructor({driver, clearIdentityAndCache, ownedHandle = null}) { this.#driver=driver; this.#clear=clearIdentityAndCache; this.#handle=ownedHandle; if(ownedHandle)this.state="READY"; }
  async command(message) {
    if (!message || Object.getPrototypeOf(message)!==Object.prototype || Object.keys(message).length!==1
        || !["start","stop","status","close","reconcile"].includes(message.action)) throw new Error("bridge command rejected");
    if(message.action==="status")return this.status();
    if(message.action==="close") { this.#closing=true; this.#clear(); }
    if(message.action==="start") {
      if(this.#closing || this.state!=="STOPPED")throw new Error("start blocked");
      this.state="STARTING";
      try {
        // Host-owned recipe, never frontend executable, argv, environment, PID or URL.
        this.#handle=await this.#driver.start(Object.freeze({binary:"ai-runtime-test",network:"loopback-only",environment:"synthetic-only"}));
        if(!this.#handle)throw new Error("missing ownership handle");
        if(this.#driver.handshake)await this.#driver.handshake(this.#handle);
        this.state="READY";
        if(this.#closing)await this.#stop();
      } catch { this.state="CLEANUP_UNKNOWN"; }
    } else if(message.action==="reconcile") {
      if(this.state!=="CLEANUP_UNKNOWN" || !this.#handle)throw new Error("no attributable read evidence");
      const proof=await this.#driver.inspect(this.#handle);
      this.#acceptExit(proof); // read-only; no repeated stop or inferred PID absence
    } else if(this.state!=="STARTING") {
      if(message.action!=="close")this.#clear();
      await this.#stop();
    } else { this.#closing=true; this.#clear(); }
    return this.status();
  }
  #acceptExit(proof) {
    if(!proof || proof.handle!==this.#handle || proof.exited!==true)throw new Error("exit evidence mismatch");
    this.#handle=null;this.state="STOPPED";
  }
  async #stop() {
    if(this.state==="STOPPED")return;
    if(this.state!=="READY" || !this.#handle)throw new Error("cleanup needs read reconciliation");
    this.state="STOPPING";
    try {this.#acceptExit(await this.#driver.stop(this.#handle));}
    catch {this.state="CLEANUP_UNKNOWN";throw new Error("cleanup unknown; no resend");}
  }
  status(){return {state:this.state,windowCloseAllowed:this.#closing && this.state==="STOPPED"};}
}
