<script setup lang="ts">
import { onBeforeUnmount, ref } from "vue";
type Proposal = { manifest_id: string; recipient: string; version: string; columns: string[]; filter_item: boolean; limit: number };
type Entry = { id: string; version: string; state: string; revision: number; maintainer: string; digest: string; reviewers: Record<string, unknown>; proposal: Proposal; generation: number | null; recipient_binding: { runtime_subject: string }; manifest: { version: string } };
type Attempt = { scope_id: string; attempt_id: string; fingerprint: string; status: string; revision: number; acknowledged: boolean; command: { action: string; registration_id: string | null; expected_revision: number; proposal: Proposal | null }; evidence: { snapshot: Entry } | null };
const tokenInput = ref(""), message = ref(""), scope = ref(""), subject = ref(""), busy = ref(false), ready = ref(false), elsewhere = ref(false);
const entries = ref<Entry[]>([]), selected = ref<Entry | null>(null), active = ref<Attempt | null>(null), ackPreview = ref(false);
const roles = ref<string[]>([]), actor = ref("");
const manifestId = ref(""), recipient = ref("alice");
const version = ref("p2"), limit = ref(10), secret = ref(true);
const filterItem = ref(true);
let token = "", generation = 0, controller: AbortController | null = null;
const demoRole = ref("policy_maintainer");
let demoTokens: Record<string, string> = {}, demoExpires = 0;
function clear() { generation++; controller?.abort(); controller = null; entries.value = []; selected.value = null; active.value = null; scope.value = ""; subject.value = ""; roles.value = []; actor.value = ""; ready.value = false; elsewhere.value = false; ackPreview.value = false; token = ""; busy.value = false; }
onBeforeUnmount(() => { clear(); demoTokens = {}; });
async function call(path: string, body?: unknown) {
  const response = await fetch("/v1/" + path, { method: body === undefined ? "GET" : "POST", headers: { Authorization: "Bearer " + token, ...(body === undefined ? {} : { "Content-Type": "application/json" }) }, body: body === undefined ? undefined : JSON.stringify(body), signal: controller?.signal, credentials: "omit", cache: "no-store", redirect: "error" });
  if (!response.ok) throw new Error(`Runtime HTTP ${response.status}`);
  return response.json();
}
async function apply() { const value = tokenInput.value; clear(); tokenInput.value = ""; token = value; await refresh(); }
async function refresh() {
  if (busy.value || !token) return;
  const current = generation; busy.value = true; ready.value = false; roles.value = []; entries.value = []; selected.value = null; active.value = null; ackPreview.value = false; controller = new AbortController();
  try {
    const identity = await call("workbench/identity");
    if (current !== generation) return;
    if (identity.auth_method !== "user" || typeof identity.scope_id !== "string") throw new Error("验签身份无效");
    scope.value = identity.scope_id; actor.value = identity.subject; subject.value = `${identity.tenant_id} / ${identity.subject}`;
    const ledger = await call("isolated-policies/ledger");
    if (current !== generation) return;
    if (ledger.scope_id !== scope.value || typeof ledger.blocked_elsewhere !== "boolean") throw new Error("账本身份不匹配");
    if (ledger.active && (ledger.active.scope_id !== scope.value || !["PREPARED", "COMMITTED", "REJECTED_NO_EFFECT"].includes(ledger.active.status))) throw new Error("回执身份或状态不匹配");
    active.value = ledger.active; elsewhere.value = ledger.blocked_elsewhere; ready.value = true;
    message.value = "已只读核对主体账本；终结回执仍需本人显式收口。";
    const listing = await call("isolated-policies");
    if (current !== generation) return;
    if (listing.identity.scope_id !== scope.value || !Array.isArray(listing.items)) throw new Error("清单范围不匹配");
    entries.value = listing.items; roles.value = listing.roles;
  } catch (error) { if (current === generation) message.value = String(error) + "；读取失败不表示没有写入，不自动重发。"; }
  finally { if (current === generation) busy.value = false; }
}
function canAction(action: string) {
  const row = selected.value;
  if (action === "draft") return roles.value.includes("data-policy-maintainer");
  if (!row) return false;
  if (["approve", "reject"].includes(action)) return roles.value.includes("data-policy-reviewer") && row.state === "submitted" && row.maintainer !== actor.value && row.recipient_binding.runtime_subject !== actor.value && !row.reviewers[actor.value];
  if (!roles.value.includes("data-policy-maintainer") || row.maintainer !== actor.value) return false;
  return action === "publish" ? row.state === "submitted" && Object.keys(row.reviewers).length >= 2
    : action === "revoke" ? row.state === "active" : action === "withdraw" ? ["submitted", "rejected"].includes(row.state) : row.state === "draft";
}
async function mutate(action: string) {
  if (!ready.value || busy.value || active.value || elsewhere.value || !canAction(action)) return;
  const current = generation;
  const proposal = { manifest_id: manifestId.value, recipient: recipient.value, version: action === "edit" ? selected.value?.version : version.value, columns: secret.value ? ["item", "secret"] : ["item"], filter_item: filterItem.value, limit: limit.value };
  const command = { attempt_id: crypto.randomUUID(), action, registration_id: action === "draft" ? null : selected.value?.id,
    expected_revision: action === "draft" ? 0 : selected.value?.revision, proposal: ["draft", "edit"].includes(action) ? proposal : null };
  busy.value = true; ready.value = false;
  try { await call("isolated-policies/prepare", command); if (current === generation) message.value = "已登记，尚未执行；请只读恢复并核对。"; }
  catch (error) { if (current === generation) message.value = String(error) + "；登记结果需只读核对，不自动重发。"; }
  finally { if (current === generation) busy.value = false; }
}
async function settle(ack: boolean) {
  const row = active.value;
  if (!row || !ready.value || busy.value || row.scope_id !== scope.value) return;
  if (ack && !ackPreview.value) return;
  if (ack ? !["COMMITTED", "REJECTED_NO_EFFECT"].includes(row.status) : row.status !== "PREPARED") return;
  const current = generation; busy.value = true; ready.value = false; ackPreview.value = false;
  try { await call(`isolated-policies/${row.attempt_id}/${ack ? 'acknowledge' : 'execute'}`, { fingerprint: row.fingerprint, revision: row.revision }); if (current === generation) message.value = "操作响应已收到，请只读恢复核对持久回执。"; }
  catch (error) { if (current === generation) message.value = String(error) + "；结果未知，保持冻结，只读恢复。"; }
  finally { if (current === generation) busy.value = false; }
}
async function loadDemo() {
  try {
    if (location.hostname !== "127.0.0.1") throw new Error();
    const response = await fetch("/__demo/info", { credentials: "omit", cache: "no-store", redirect: "error" });
    if (!response.ok) throw new Error();
    const value = await response.json(), expires = Date.parse(value.expires_at);
    if (!Number.isFinite(expires) || expires <= Date.now() || expires - Date.now() > 1800000 || ["policy_maintainer", "policy_reviewer_a", "policy_reviewer_b"].some(k => typeof value.policy_tokens?.[k] !== "string")) throw new Error();
    demoTokens = value.policy_tokens; demoExpires = expires; message.value = "隔离角色已载入内存，选择后显式应用；后端仍须验签。";
  } catch { demoTokens = {}; message.value = "隔离管理身份不可用；不使用开发头替代。"; }
}
async function applyDemo() { if (Date.now() >= demoExpires || !demoTokens[demoRole.value]) { clear(); message.value = "请重新加载有效的隔离测试身份。"; return; } tokenInput.value = demoTokens[demoRole.value]; await apply(); }
</script>
<template>
<section class="panel" aria-label="隔离策略管理">
  <h3>隔离策略管理 · 双人审核</h3>
  <p>仅临时库启用。独立维护/审核角色，管理员不自动获权；策略绑定已发布清单与一个测试用户；不会自动继承旧授权。签名下载暂缓。</p>
  <button @click="loadDemo">加载隔离管理测试身份</button><label>策略测试角色<select v-model="demoRole"><option value="policy_maintainer">维护者</option><option value="policy_reviewer_a">审核者A</option><option value="policy_reviewer_b">审核者B</option></select></label><button @click="applyDemo">应用策略测试角色</button>
  <label>策略管理用户令牌<input v-model="tokenInput" type="password" autocomplete="off" /></label><button @click="apply">应用策略身份并清空</button>
  <p>验签身份：{{ subject || '未核对' }}</p><p role="status">{{ message }}</p>
  <button :disabled="busy" @click="refresh">只读恢复策略账本与清单</button>
  <p v-if="elsewhere">同主体存在其他工作台未决写，请回原模块核对并本人ACK；不能在此解锁。</p>
  <section v-if="active" aria-label="策略操作回执">
    <p>{{ active.attempt_id }} · {{ active.status }} · {{ active.command.action }} · 版本 {{ active.revision }}</p><pre>{{ active.command }}</pre><pre v-if="active.evidence">{{ active.evidence }}</pre>
    <button v-if="active.status === 'PREPARED'" :disabled="busy || !ready" @click="settle(false)">执行已登记策略操作一次</button>
    <button v-else :disabled="busy || !ready" @click="ackPreview = true">预览策略本人收口</button>
    <section v-if="ackPreview"><p>仅确认此attempt、指纹与回执版本，不重发操作。</p><button :disabled="busy || !ready" @click="settle(true)">确认策略本人收口</button><button @click="ackPreview = false">取消策略收口</button></section>
  </section>
  <fieldset :disabled="busy || !ready || !!active || elsewhere"><legend>显式登记，不自动执行</legend>
    <label>绑定清单ID<input v-model="manifestId" placeholder="从注册详情复制view_…" /></label><label>受益测试用户<select v-model="recipient"><option value="alice">Alice</option><option value="bob">Bob</option></select></label>
    <label>策略新版本<input v-model="version" maxlength="64" /></label><label>策略限行<input v-model.number="limit" type="number" min="1" max="100" /></label><label><input v-model="secret" type="checkbox" />保留secret遮盖列</label>
    <label><input v-model="filterItem" type="checkbox" />策略允许item等值过滤</label>
    <button :disabled="!canAction('draft')" @click="mutate('draft')">登记创建策略草稿</button>
    <template v-if="selected"><button v-for="action in ['edit','submit','approve','reject','withdraw','publish','revoke']" :key="action" :disabled="!canAction(action)" @click="mutate(action)">登记 {{ action }}</button></template>
  </fieldset>
  <ul><li v-for="item in entries" :key="item.id"><button @click="selected = item; manifestId = item.proposal.manifest_id; recipient = item.proposal.recipient; limit = item.proposal.limit; secret = item.proposal.columns.includes('secret'); filterItem = item.proposal.filter_item">{{ item.version }} · {{ item.state }} · r{{ item.revision }}</button></li></ul>
  <section v-if="selected" aria-label="策略详情"><h4>{{ selected.version }} · {{ selected.state }}</h4><p>维护者 {{ selected.maintainer }}；审核 {{ Object.keys(selected.reviewers).length }} 人；代次 {{ selected.generation ?? '尚未发布' }}</p><p>查询清单版本：{{ selected.manifest.version }}；策略active仍须与当前清单匹配，撤销或替换后失败关闭。</p><pre>{{ selected }}</pre></section>
</section>
</template>
