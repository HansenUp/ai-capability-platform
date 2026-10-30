import { ref, type InjectionKey } from "vue";

export function createDemoContext() {
  const role = ref(""), agent = ref("s12-workbench"), run = ref(""), epoch = ref(0), message = ref("演示环境信息未读取"), ready = ref(false);
  const alertsReady = ref(false);
  let alertTokens: Record<string, string> = {};
  let tokens: Record<string, string> = {}, expires = 0, timer: ReturnType<typeof setTimeout> | null = null, sequence = 0, closed = false;
  function clear() { alertTokens = {}; alertsReady.value = false; tokens = {}; role.value = ""; ready.value = false; epoch.value++; if (timer) clearTimeout(timer); timer = null; }
  function select(value: string) {
    if (!ready.value || Date.now() >= expires || !["alice", "bob", ""].includes(value)) { clear(); return; }
    role.value = value; epoch.value++;
  }
  async function load() {
    const current = ++sequence; clear(); message.value = "正在读取隔离测试身份…";
    try {
      if (location.hostname !== "127.0.0.1") throw new Error();
      const response = await fetch("/__demo/info", { method: "GET", credentials: "omit", cache: "no-store", redirect: "error" });
      if (!response.ok) throw new Error();
      const body = await response.json();
      if (closed || current !== sequence) return;
      const until = Date.parse(body.expires_at);
      if (!Number.isFinite(until) || until <= Date.now() || until - Date.now() > 30 * 60000
        || !body.tokens || ["alice", "bob"].some(name => typeof body.tokens[name] !== "string"
          || !body.tokens[name] || body.tokens[name].length > 16384 || /\s/.test(body.tokens[name]))) throw new Error();
      if (body.alert_tokens !== undefined) {
        if (["alice", "bob"].some(name => typeof body.alert_tokens?.[name] !== "string" || !body.alert_tokens[name]
          || body.alert_tokens[name].length > 16384 || /\s/.test(body.alert_tokens[name]))) throw new Error();
        alertTokens = { alice: body.alert_tokens.alice, bob: body.alert_tokens.bob }; alertsReady.value = true;
      }
      tokens = { alice: body.tokens.alice, bob: body.tokens.bob }; expires = until; ready.value = true;
      message.value = `独立演示有效至 ${new Date(expires).toLocaleTimeString()}；身份仅供本次临时Runtime，仍须服务端验签。`;
      timer = setTimeout(() => { clear(); message.value = "演示身份已到期，请停止或重新启动独立环境。"; }, expires - Date.now());
    } catch { if (!closed && current === sequence) { clear(); message.value = "隔离演示信息不可用；不会使用正式身份或自动重试。"; } }
  }
  return { role, agent, run, epoch, message, ready, alertsReady, load, select,
    alertToken(name: string) { if (Date.now() >= expires) { clear(); return ""; } return ["alice", "bob"].includes(name) ? alertTokens[name] ?? "" : ""; },
    token() { if (Date.now() >= expires) { clear(); return ""; } return tokens[role.value] ?? ""; },
    dispose() { closed = true; sequence++; clear(); },
  };
}
export type DemoContext = ReturnType<typeof createDemoContext>;
export const demoContextKey: InjectionKey<DemoContext> = Symbol("isolated-demo-context");
