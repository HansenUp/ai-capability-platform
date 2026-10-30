<script setup lang="ts">
import { inject, onBeforeUnmount, ref } from "vue";
import { demoContextKey } from "../demo-context";
const demo = inject(demoContextKey, null);
const demoActor = ref("alice");
function applyDemoAlert() { if (!demo) return; draft.value = demo.alertToken(demoActor.value); reset(); }
interface AlertRow { id: string; tenant_id: string; node_id: string; rule: string; evidence: string; signal: string; handling: string; assignee: string | null; revision: number; synthetic: boolean; history: Record<string, unknown>[]; notifications: Record<string, unknown>[] }
interface Action { operation_id: string; revision: number; action: "claim" | "handle" | "notify"; reason?: string; evidence?: string }
const draft = ref(""), token = ref(""), tenant = ref(""), subject = ref("");
const rows = ref<AlertRow[]>([]), selected = ref<AlertRow | null>(null), message = ref("");
const busy = ref(false), pending = ref<Action | null>(null), unknown = ref<Action | null>(null), unknownAlert = ref("");
const reason = ref("investigated");
let generation = 0;
function reset() { generation++; token.value = draft.value; draft.value = ""; rows.value = []; selected.value = null; tenant.value = subject.value = ""; pending.value = null; unknown.value = null; message.value = "身份已切换，需重新读取；不会重发任何操作。"; busy.value = false; }
async function request(path: string, body?: Action) {
  const response = await fetch(`/console/isolated-alerts${path}`, { method: body ? "POST" : "GET", cache: "no-store",
    headers: { Authorization: `Bearer ${token.value}`, ...(body ? { "Content-Type": "application/json" } : {}) }, body: body ? JSON.stringify(body) : undefined });
  if (!response.ok) throw new Error(`HTTP ${response.status}：读取不可用或操作未获明确成功；不自动重试。`);
  return response.json();
}
async function read(id?: string) {
  const current = generation; busy.value = true; message.value = ""; selected.value = null; pending.value = null;
  if (!id) rows.value = [];
  try {
    const result = await request(id ? `/${encodeURIComponent(id)}` : "");
    if (current !== generation) return;
    if (result.synthetic !== true) throw new Error("不是隔离数据");
    if (id) {
      if (result.id !== id || result.tenant_id !== tenant.value) throw new Error("告警范围不匹配");
      selected.value = result;
    } else { tenant.value = result.tenant_id; subject.value = result.subject; rows.value = result.items; }
  } catch (error) { if (current === generation) message.value = String(error); }
  finally { if (current === generation) busy.value = false; }
}
function preview(action: Action["action"]) {
  if (!selected.value || unknown.value || busy.value) return;
  pending.value = { operation_id: crypto.randomUUID(), revision: selected.value.revision, action,
    ...(action === "handle" ? { reason: reason.value, evidence: selected.value.evidence } : {}) };
}
async function confirm() {
  const row = selected.value, action = pending.value;
  if (!row || !action || busy.value || unknown.value) return;
  const current = generation; pending.value = null; unknown.value = action; unknownAlert.value = row.id; busy.value = true;
  try {
    const receipt = await request(`/${encodeURIComponent(row.id)}/actions`, action);
    if (current !== generation) return;
    verify(receipt, action, row.id);
    unknown.value = null; await read(row.id); if (current === generation) message.value = "操作已记录；信号状态未因人工处理改变。";
  } catch (error) { if (current === generation) message.value = `${error} 结果未知，仅可读取原操作回执。`; }
  finally { if (current === generation) busy.value = false; }
}
function verify(receipt: any, action: Action, id: string) {
  if (receipt.synthetic !== true || receipt.tenant_id !== tenant.value || receipt.subject !== subject.value || receipt.alert_id !== id || receipt.status !== "committed"
    || receipt.revision !== action.revision + 1 || receipt.request.operation_id !== action.operation_id || receipt.request.revision !== action.revision
    || receipt.request.action !== action.action || (receipt.request.reason ?? null) !== (action.reason ?? null) || (receipt.request.evidence ?? null) !== (action.evidence ?? null)) throw new Error("回执不匹配，保持未知");
}
async function recover() {
  const action = unknown.value, id = unknownAlert.value, current = generation;
  if (!action || busy.value) return;
  busy.value = true;
  try {
    const receipt = await request(`/operations/${action.operation_id}`);
    if (current !== generation) return;
    verify(receipt, action, id); unknown.value = null; await read(id); if (current === generation) message.value = "已只读核对原操作回执，未重发操作。";
  } catch (error) { if (current === generation) message.value = `${error} 无回执不代表没有写入，保持未知。`; }
  finally { if (current === generation) busy.value = false; }
}
onBeforeUnmount(() => { generation++; token.value = ""; });
</script>
<template>
  <h2>合成节点告警 · 隔离工作台</h2>
  <p>仅临时库与合成信号；没有真实节点采集或通知发送。认领、处理与信号恢复互相独立。北极星仍待定义。</p>
  <section v-if="demo" aria-label="告警演示身份与指引">
    <h3>无需复制令牌的隔离体验</h3>
    <label>告警演示管理员 <select v-model="demoActor" aria-label="告警演示管理员"><option value="alice">Alice：本人认领与处理</option><option value="bob">Bob：查看同租户，不能代处理</option></select></label>
    <button :disabled="!demo.alertsReady.value || busy" @click="applyDemoAlert">应用告警演示身份</button>
    <p v-if="!demo.alertsReady.value">告警测试身份不可用或已到期；请重新启动隔离演示，不回退到开发身份头。</p>
    <ol><li>选择 Alice 并应用身份，读取列表、打开详情。</li><li>预览本人认领并确认，再预览显式处理并确认；信号不会自动恢复。</li><li>预览模拟通知并确认，在详情查看处理与通知历史。</li><li>切换 Bob 并重新读取：可查看同租户记录，不能代替 Alice 处理。</li></ol>
    <p>选择下拉项不会自动应用或发送请求。令牌仅在内存，服务端仍验签；未知结果先只读核对原回执，不重发。演示到期后临时数据不可恢复。</p>
  </section>
  <label>告警测试管理员令牌 <input v-model="draft" type="password" autocomplete="off" /></label>
  <button @click="reset">应用告警身份并清空</button>
  <button :disabled="!token || busy" @click="read()">读取告警列表</button>
  <p v-if="tenant">验签身份：{{ tenant }} / {{ subject }}</p>
  <p role="status">{{ message }}</p>
  <p v-if="unknown" role="alert">操作结果未知：{{ unknown.operation_id }}。禁止再次提交；刷新或无回执不代表未写入。<button :disabled="busy" @click="recover">只读核对原操作回执</button></p>
  <ul><li v-for="row in rows" :key="row.id">{{ row.node_id }} · {{ row.signal }} / {{ row.handling }} <button :disabled="busy" @click="read(row.id)">查看告警 {{ row.id }}</button></li></ul>
  <p v-if="!rows.length">当前没有可展示记录；未读取或读取失败不代表没有告警。</p>
  <section v-if="selected" aria-label="告警详情">
    <h3>{{ selected.id }}</h3>
    <p>信号：{{ selected.signal }} · 处理：{{ selected.handling }} · 修订 {{ selected.revision }}</p>
    <p>责任人：{{ selected.assignee ?? '未认领' }} · 证据：{{ selected.evidence }}</p>
    <p v-if="selected.handling === 'handled' && selected.signal !== 'recovered'">已处理，但信号尚未证实恢复。</p>
    <fieldset :disabled="busy || !!unknown || !!pending">
      <button v-if="selected.handling === 'unclaimed'" @click="preview('claim')">预览本人认领</button>
      <template v-if="selected.assignee === subject">
        <label>处理原因 <select v-model="reason"><option value="investigated">已核查证据</option><option value="no_action_needed">核查后无需动作</option></select></label>
        <button v-if="selected.handling === 'claimed'" @click="preview('handle')">预览显式处理</button>
        <button @click="preview('notify')">预览模拟通知</button>
      </template>
    </fieldset>
    <h4>处理历史</h4><ul><li v-for="entry in selected.history" :key="String(entry.operation_id)">{{ entry.action }} · {{ entry.subject }} · {{ entry.at }} · {{ entry.reason }} · {{ entry.evidence }}</li></ul>
    <h4>模拟通知历史</h4><p>simulated_received 仅表示纯替身接收，不代表真人送达或已读；failed / unknown 不自动重投。</p>
    <ul><li v-for="entry in selected.notifications" :key="String(entry.operation_id)">{{ entry.status }} · {{ entry.channel }} · {{ entry.at }}</li></ul>
  </section>
  <section v-if="pending" aria-label="告警操作确认">
    <p>即将 {{ pending.action }} · {{ selected?.id }} · 修订 {{ pending.revision }} · 操作 {{ pending.operation_id }}。仅记录隔离状态，不改变真实节点或业务Run。</p>
    <p v-if="pending.action === 'notify'">通知预览：合成节点健康异常 · 仅内存替身渠道；不包含令牌、业务正文或外部地址。</p>
    <button :disabled="busy" @click="confirm">确认记录一次</button><button :disabled="busy" @click="pending = null">取消告警操作</button>
  </section>
</template>
