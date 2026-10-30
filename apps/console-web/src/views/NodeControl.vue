<script setup lang="ts">
import { ref, onBeforeUnmount } from "vue";
interface NodeState { http_concurrency_limit?: number | null; id: string; generation: number; state: string; desired: string; control_busy: boolean; can_resume: boolean; recovery_attempt?: string | null }
interface Operation { limit?: number; id: string; node: string; actor: string; action: string; status: string; version: number; acknowledged: boolean }
const draft = ref(""), token = ref(""), actor = ref(""), label = ref(""), message = ref("");
const nodes = ref<NodeState[]>([]), operations = ref<Operation[]>([]);
const busy = ref(false), ready = ref(false), fresh = ref(""), unknown = ref("");
const limits = ref<Record<string, number>>({});
let epoch = 0;
function changeIdentity() {
  limits.value = {}; epoch++; token.value = draft.value; draft.value = ""; actor.value = label.value = "";
  nodes.value = []; operations.value = []; ready.value = false; busy.value = false; fresh.value = unknown.value = "";
  message.value = "身份已切换，先读取权威状态；不会重发操作。";
}
onBeforeUnmount(() => { epoch++; token.value = draft.value = ""; });
async function request(path: string, body?: unknown) {
  const res = await fetch(`/console/isolated-node-control${path}`, { method: body === undefined ? "GET" : "POST", cache: "no-store", redirect: "error",
    headers: { Authorization: `Bearer ${token.value}`, ...(body === undefined ? {} : { "Content-Type": "application/json" }) }, body: body === undefined ? undefined : JSON.stringify(body) });
  if (!res.ok) throw new Error(`HTTP ${res.status}：结果未确定，请只读恢复，不自动重发或解锁。`);
  return res.json();
}
function valid(op: Operation, identity: string) { return op && op.actor === identity && op.version === 1 && typeof op.id === "string" && ["PREPARED","EXECUTING","SUCCEEDED","FAILED","CANCELLED"].includes(op.status); }
async function load() {
  const current = epoch; busy.value = true; ready.value = false; nodes.value = []; operations.value = []; message.value = "";
  try {
    const snapshot = await request("");
    if (current !== epoch) return;
    if (snapshot.synthetic !== true || ![snapshot.issuer,snapshot.tenant_id,snapshot.subject].every(v => typeof v === "string" && v)) throw new Error("验签范围不可用");
    const identity = JSON.stringify([snapshot.issuer,snapshot.tenant_id,snapshot.subject]);
    const recovered = await request("/operations");
    if (current !== epoch) return;
    if (recovered.synthetic !== true || !Array.isArray(recovered.operations) || !recovered.operations.every((op: Operation) => valid(op,identity))) throw new Error("未决记录范围不匹配");
    if (!Array.isArray(snapshot.nodes) || !snapshot.nodes.every((n: NodeState) => typeof n.id === "string" && Number.isSafeInteger(n.generation) && typeof n.control_busy === "boolean")) throw new Error("节点状态不可用");
    if (unknown.value) {
      const op = await request(`/operations/${encodeURIComponent(unknown.value)}`);
      if (current !== epoch) return;
      if (!valid(op,identity) || op.id !== unknown.value) throw new Error("原操作证据不匹配");
      unknown.value = "";
    }
    actor.value = identity; label.value = `${snapshot.tenant_id} / ${snapshot.subject}`;
    nodes.value = snapshot.nodes; operations.value = recovered.operations; ready.value = true;
  } catch (error) { if (current === epoch) message.value = String(error); }
  finally { if (current === epoch) busy.value = false; }
}
async function readOriginal(id: string) {
  const current = epoch; busy.value = true;
  try {
    const op = await request(`/operations/${encodeURIComponent(id)}`);
    if (current !== epoch) return;
    if (!valid(op,actor.value) || op.id !== id) throw new Error("原操作范围不匹配");
    operations.value = [...operations.value.filter(item => item.id !== id), op];
  } catch (error) { if (current === epoch) message.value = String(error); }
  finally { if (current === epoch) busy.value = false; }
}
async function prepare(node: NodeState, action: string) {
  const limit = action === "configure_limit" ? limits.value[node.id] : undefined;
  if (action === "configure_limit" && (!Number.isSafeInteger(limit) || (limit ?? 0) <= 0)) { message.value = "并发上限必须是正整数"; return; }
  const id = crypto.randomUUID(), current = epoch; busy.value = true; ready.value = false; unknown.value = id;
  try {
    const op = await request("/prepare",{ attempt:id,node:node.id,action,expected:node.generation,...(limit === undefined ? {} : {limit}) });
    if (current !== epoch) return;
    if (!valid(op,actor.value) || op.id !== id || op.node !== node.id || op.action !== action || op.limit !== limit) throw new Error("登记范围不匹配");
    fresh.value = id; unknown.value = ""; await load();
  } catch (error) { if (current === epoch) message.value = String(error); }
  finally { if (current === epoch) busy.value = false; }
}
async function act(op: Operation, action: string) {
  const current = epoch; busy.value = true; ready.value = false; unknown.value = op.id;
  try {
    await request(`/operations/${encodeURIComponent(op.id)}/${action}`, action === "ack" ? {receipt:op} : {});
    if (current !== epoch) return;
    fresh.value = ""; await load();
  } catch (error) { if (current === epoch) message.value = String(error); }
  finally { if (current === epoch) busy.value = false; }
}
</script>
<template>
<section><h2>独立排空／恢复 · 隔离实验</h2>
<p>仅签名测试管理员与合成节点。排空停止新准入，不终止在途任务；超时保持关闭。ACK不恢复节点。</p>
<RouterLink to="/manage/nodes">返回节点历史与诊断</RouterLink>
<form @submit.prevent="changeIdentity"><label>测试用户令牌 <input v-model="draft" type="password" autocomplete="off" /></label><button>应用身份</button></form>
<button :disabled="busy || !token" @click="load">读取权威状态与未决操作</button>
<p v-if="label">验签身份：{{ label }}</p><p v-if="message" role="alert">{{ message }}</p>
<p>重载后重新认证并读取；恢复的PREPARED仅允许撤销。未知结果只读对账，不重新执行，不以空列表推断解锁。</p>
<article v-for="node in nodes" :key="node.id"><h3>{{ node.id }}</h3><p>代际 {{ node.generation }} · {{ node.state }} · 期望 {{ node.desired }} · {{ node.control_busy ? '控制写已锁定' : '服务端允许新登记' }}</p>
<button :disabled="!ready || busy || node.control_busy || node.state !== 'SERVING'" @click="prepare(node,'drain')">登记排空</button>
<button v-if="node.recovery_attempt" :disabled="busy || !ready" @click="readOriginal(node.recovery_attempt)">读取原排空回执</button>
<button :disabled="!ready || busy || !node.can_resume" @click="prepare(node,'resume')">登记恢复</button>
<p>HTTP／SSE并发上限：{{ node.http_concurrency_limit ?? '未配置' }}（单进程；不限制Worker；超限429，不排队）</p>
<label>新并发上限 <input v-model.number="limits[node.id]" type="number" min="1" step="1" :disabled="busy || node.control_busy" /></label>
<button :disabled="!ready || busy || node.control_busy || !['SERVING','DRAINED'].includes(node.state)" @click="prepare(node,'configure_limit')">登记并发上限</button></article>
<article v-for="op in operations" :key="op.id"><h3>操作 {{ op.id }}</h3><p>{{ op.node }} / {{ op.action }} / {{ op.status }}<span v-if="op.action === 'configure_limit'"> · 上限 {{ op.limit }}</span></p>
<button v-if="op.status === 'PREPARED' && fresh === op.id" :disabled="busy || !ready" @click="act(op,'execute')">显式执行一次</button>
<button v-if="op.status === 'PREPARED'" :disabled="busy || !ready" @click="act(op,'withdraw')">本人撤销登记</button>
<button v-if="op.status === 'EXECUTING'" :disabled="busy || !ready" @click="act(op,'reconcile')">只读对账原结果</button>
<button v-if="op.status === 'FAILED' && op.action === 'drain'" :disabled="busy || !ready" @click="act(op,'observe')">读取自然排空观测</button>
<button v-if="!op.acknowledged && ['SUCCEEDED','FAILED','CANCELLED'].includes(op.status)" :disabled="busy || !ready" @click="act(op,'ack')">本人确认收口 ACK</button></article>
</section>
</template>
<style scoped>
section { max-width:960px; } article { border:1px solid #cbd5e1; border-radius:12px; padding:16px; margin:16px 0; overflow-wrap:anywhere; }
form { margin:20px 0; } button { margin:4px; min-height:44px; } p { line-height:1.7; }
@media(max-width:600px) { label { display:grid; gap:8px; } }
</style>
