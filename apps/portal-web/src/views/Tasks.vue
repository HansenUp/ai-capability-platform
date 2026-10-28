<script setup lang="ts">
import { inject, onBeforeUnmount, ref, watch } from "vue";
import { RuntimeRunClient, type WorkbenchRun } from "@ai-platform/sdk-web/run-client";
import { getToken } from "../auth";
import { portalTokenKey } from "../runtime-session";
const tokenProvider = inject(portalTokenKey, getToken);
const agent = ref(""), state = ref(""), message = ref("尚未应用身份，请显式读取本人任务。"), identity = ref("");
const rows = ref<WorkbenchRun[]>([]), cursor = ref<string | null>(null), busy = ref(false);
let epoch = 0, controller: AbortController | null = null;
function clear() { epoch++; controller?.abort(); controller = null; rows.value = []; cursor.value = null; identity.value = ""; busy.value = false; }
watch([agent, state], () => { clear(); message.value = "筛选已变化，请重新读取首页。"; });
async function load(next = false) {
  const nextCursor = next ? cursor.value : null;
  if (next && !nextCursor) return;
  const priorIdentity = identity.value;
  clear(); const current = epoch; const active = new AbortController(); controller = active; busy.value = true;
  message.value = "正在核对身份并只读查询…";
  try {
    const token = await tokenProvider();
    if (current !== epoch) return;
    const client = new RuntimeRunClient(async () => ({ Authorization: `Bearer ${token}` }));
    const ledger = await client.ledger(active.signal);
    if (current !== epoch) return;
    const scope = `${ledger.identity.tenant_id} / ${ledger.identity.subject} / ${ledger.identity.scope_id}`;
    if (next && scope !== priorIdentity) throw new Error("身份已变化，请重新读取首页。");
    const result = await client.listMyRuns(ledger.identity.subject, agent.value.trim(), state.value, nextCursor, active.signal);
    if (current !== epoch) return;
    rows.value = result.items; cursor.value = result.next_cursor; identity.value = scope;
    message.value = ledger.active ? "本人任务已读取；存在未决写锁，请在工作台只读恢复并由本人确认，不自动解锁。" : "本人任务已读取；详情中仍须重新应用身份并核对账本。";
  } catch (error) { if (current === epoch) message.value = String(error); }
  finally { if (current === epoch) { busy.value = false; controller = null; } }
}
onBeforeUnmount(clear);
</script>
<template>
  <router-link to="/">返回可用 Agent</router-link>
  <h2>我的任务</h2><p>仅查询验签身份本人发起的 Run，不是审批收件箱。管理员也只查询本人；读取列表不会解除未决写锁。</p>
  <label>任务 Agent 筛选 <input v-model="agent" maxlength="64" /></label>
  <label>任务状态 <select v-model="state" aria-label="任务状态"><option value="">全部状态</option><option value="submitted">已提交</option><option value="working">运行中</option><option value="auth-required">等待审批</option><option value="input-required">等待输入</option><option value="completed">已结束</option><option value="failed">失败</option><option value="canceled">已取消</option></select></label>
  <button @click="load()">应用身份并读取任务首页</button>
  <button :disabled="busy || !cursor" @click="load(true)">读取任务下一页</button>
  <p role="status">{{ message }}</p><p v-if="identity">验签身份：{{ identity }}</p>
  <p v-if="!busy && identity && !rows.length">当前筛选没有本人任务。</p>
  <ul aria-label="本人任务列表"><li v-for="row in rows" :key="row.run_id">
    <router-link :to="{ path: `/chat/${encodeURIComponent(row.agent_id)}`, query: { run: row.run_id, from: 'tasks' } }">查看任务 {{ row.run_id }}</router-link>
    <p>{{ row.agent_id }} · {{ row.state }} · 事件序号 {{ row.last_seq }}</p>
  </li></ul>
</template>
