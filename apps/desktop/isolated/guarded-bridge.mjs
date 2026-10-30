/** Synthetic native boundary: context and pipe are trusted injected test adapters,
 * not frontend message fields. No OS, URL requests, credentials or cryptographic claim. */
import { NativeBridgeModel } from "./native-bridge.mjs";
export function createGuardedBridge({context, mainWindow, pipe, challenge, clearIdentityAndCache}) {
  let generation=0, pending=null;
  const challenges=new Set();
  function authorize(){
    const caller=context();
    if(!caller || caller.window!==mainWindow || caller.windowLabel!=="main" || caller.webviewLabel!=="main"
      || caller.source!=="packaged-local" || caller.topLevel!==true || caller.capability!=="isolated-host-v1")throw new Error("native caller rejected");
    return caller;
  }
  const bridge=new NativeBridgeModel({clearIdentityAndCache,driver:{
    start:async()=>{
      const caller=authorize();
      const nonce=challenge();
      if(typeof nonce!=="string" || !nonce || challenges.has(nonce))throw new Error("nonfresh startup challenge");
      challenges.add(nonce);
      const scope=Object.freeze({protocol:"f8-test-v1",generation:++generation,challenge:nonce});
      pending={scope,caller};
      // Symbolic allowlist is deliberately not a runnable production command.
      return pipe.start(Object.freeze({binary:"ai-runtime-test",profile:"synthetic-readonly",environment:"empty-plus-test-allowlist",scope}));
    },
    handshake:async handle=>{
      const attempt=pending;
      const proof=await pipe.handshake(handle,attempt.scope);
      if(authorize()!==attempt.caller || pending!==attempt || !proof || Object.keys(proof).sort().join(",")!=="challenge,generation,handle,protocol"
        || proof.handle!==handle || proof.protocol!==attempt.scope.protocol || proof.generation!==attempt.scope.generation
        || proof.challenge!==attempt.scope.challenge)throw new Error("startup handshake rejected");
      pending=null;
    },
    stop:handle=>pipe.stop(handle),
    inspect:handle=>pipe.inspect(handle),
  }});
  return Object.freeze({invoke:async message=>{
    const caller=authorize();
    const result=await bridge.command(message);
    if(authorize()!==caller)throw new Error("caller changed; response withheld");
    return result;
  }});
}
