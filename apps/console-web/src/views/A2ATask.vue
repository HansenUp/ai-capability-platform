<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { a2aFailure } from "../a2a-guidance";
const route=useRoute();
const extension = "urn:ai-platform:isolated:workbench:1";
const draft = ref("");
const taskId = ref(typeof route.query.task === "string" ? route.query.task : "");
const applied = ref(false);
const busy = ref(false);
const state = ref("");
const text = ref<string | null>(null);
const message = ref("");
let bearer = "", generation = 0;
let controller: AbortController | null = null;
function clear() {
  generation++; controller?.abort(); controller = null;
  state.value = ""; text.value = null; message.value = ""; busy.value = false;
}
watch([draft, taskId], clear, { flush: "sync" });
watch(()=>route.query.task,value=>{clear();taskId.value=typeof value==="string"?value:"";});
watch(draft, value => { if (value) { bearer = ""; applied.value = false; } }, { flush: "sync" });
function apply() {
  const token = draft.value.trim(); clear(); bearer = token; draft.value = "";
  applied.value = !!token;
  message.value = token ? "身份已应用；令牌仅保存在本页内存。" : "请填写验签用户令牌。";
}
async function read() {
  clear();
  if (!applied.value || !taskId.value.trim()) { message.value = "请先应用身份并填写 Task ID。"; return; }
  const current = generation, id = taskId.value.trim();
  const active = new AbortController(); controller = active; busy.value = true;
  const timeout = setTimeout(() => active.abort(), 15000);
  try {
    const response = await fetch(`/v1/isolated-a2a/tasks/${encodeURIComponent(id)}`, {
      headers: { Authorization: `Bearer ${bearer}`, "A2A-Version": "1.0", "A2A-Extensions": extension },
      signal: active.signal, cache: "no-store", credentials: "omit", redirect: "error",
    });
    if (!response.ok) throw new Error(a2aFailure(response.status)+"旧结果已清空。");
    const value = await response.json();
    if (generation !== current) return;
    if (value.id !== id || typeof value.status?.state !== "string") throw new Error("响应范围不匹配，未展示结果。");
    state.value = value.status.state;
    const artifacts = value.artifacts;
    if (artifacts !== undefined) {
      const artifact = Array.isArray(artifacts) && artifacts.length === 1 ? artifacts[0] : null;
      if (value.status.state !== "TASK_STATE_COMPLETED" || artifact?.parts?.length !== 1 ||
          typeof artifact.parts[0].text !== "string" || artifact.parts[0].text.length > 65536 ||
          artifact.metadata?.[extension]?.projection !== "final-assistant-text-v1") throw new Error("结果投影格式不匹配。");
      text.value = artifact.parts[0].text;
    }
    message.value = "只读查询完成；未执行业务写或确认收口。";
  } catch (error) {
    if (generation === current) { state.value = ""; text.value = null; message.value = String(error); }
  } finally { clearTimeout(timeout); if (generation === current) { busy.value = false; controller = null; } }
}
onBeforeUnmount(() => { clear(); bearer = ""; draft.value = ""; });
</script>
<template>
  <section>
    <h1>A2A Task 只读结果</h1>
    <router-link :to="{path:'/a2a-host',query:typeof route.query.attempt==='string'?{attempt:route.query.attempt}:{}}">返回宿主核对回执与本人ACK</router-link>
    <p>导航仅携带定位ID，不携带令牌或授权。重新应用本人身份，再显式只读查询；不会自动请求。</p>
    <p>仅隔离受限接口。只允许原发起人读取已映射 Task；普通环境未启用此接口。</p>
    <details>
      <summary>完整体验顺序与恢复边界</summary>
      <ol>
        <li>在隔离协议宿主读取可见目录与受限 Agent Card，再显式登记消息；PREPARED 尚未创建 Task。</li>
        <li>核对登记后只发送一次；创建回执 COMMITTED 不等于 Run 完成。</li>
        <li>响应丢失或重载后，先 GET /v1/workbench/attempts 查本人活动锁，再 GET /v1/isolated-a2a/attempts/{attempt_id}；不要重新登记或发送。</li>
        <li>取得 Task ID 后在本页只读查询。旧 Run 或证据缺失时可能没有最终文本。</li>
        <li>回到原协议宿主，由本人核对终结回执并显式 ACK。ACK 响应丢失只读核对 acknowledged；本页不会收口。</li>
      </ol>
      <p>登记、执行和 ACK 可使用 A2A 隔离宿主页面；不要用旧工作台 execute/ACK 接口替代。取消、续发、推送与流式接入仍未启用。</p>
    </details>
    <label>用户访问令牌 <input v-model="draft" type="password" autocomplete="off" /></label>
    <button type="button" @click="apply">应用身份并清空旧结果</button>
    <label>Task ID <input v-model="taskId" autocomplete="off" /></label>
    <button type="button" :disabled="busy || !applied" @click="read">只读查询 Task</button>
    <p role="status">{{ message }}</p>
    <p v-if="state">权威状态：{{ state }}</p>
    <section v-if="state" aria-label="最终助手结果">
      <h2>最终助手文本</h2>
      <pre v-if="text !== null" class="task-result">{{ text }}</pre>
      <p v-else>暂无可核验的最终文本；不回溯中间回答，不把缺失当作空结果。</p>
      <p>完成仅表示 Run 结束，不保证每个工具成功或答案正确；max_tokens 可能截断输出。</p>
    </section>
    <p>读取或完成状态不会解锁未决写。请由原操作者在原宿主核对回执后显式 ACK；本页没有写入、重试或 ACK 操作。</p>
  </section>
</template>
<style scoped>
.task-result { white-space: pre-wrap; overflow-wrap: anywhere; max-width: 100%; }
label { display: block; margin: 1rem 0; }
input { max-width: 100%; }
</style>
