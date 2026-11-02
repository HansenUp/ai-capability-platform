<script setup lang="ts">
import {onBeforeUnmount, ref, watch} from "vue";
const props = defineProps<{token: string; agent: string}>();
type Summary = {attempt_id: string; created_at: string; status: string; acknowledged: boolean; agent_version: string; dataset_version: string};
type Detail = {attempt_id: string; scope_id: string; status: string; acknowledged: boolean;
  source: string; publish_eligible: boolean; binding: {agent_id: string; agent_version: string; dataset_version: string};
  receipt: null | {result: {selected_count?: number; verified_count?: number; passed_count?: number; pass_rate?: number | null}};
  children: {case_id: string; run_id: string; evidence: null | {judgement: string; reasons: string[]}}[]};
type Comparison = {comparable: boolean; reasons: string[]; pass_rate_delta: number | null};
const items = ref<Summary[]>([]), details = ref<Detail[]>([]), selected = ref<string[]>([]);
const next = ref<string | null>(null), status = ref(""), busy = ref(false), message = ref("");
const comparison = ref<Comparison | null>(null);
let generation = 0, controller: AbortController | null = null;
function clear() {
  items.value = []; details.value = []; selected.value = []; next.value = null; comparison.value = null;
}
function reset() { generation++; controller?.abort(); busy.value = false; clear(); message.value = ""; }
watch(() => [props.token, props.agent, status.value], reset, {flush: "sync"});
onBeforeUnmount(reset);
async function read(kind: "list" | "detail" | "compare", cursor?: string, id?: string) {
  if (busy.value || !props.token) return;
  const chosen = [...selected.value], current = ++generation, token = props.token, agent = props.agent;
  controller?.abort(); const abort = new AbortController(); controller = abort;
  busy.value = true; details.value = []; comparison.value = null; message.value = "";
  if (kind === "list") clear();
  async function get(path: string) {
    const response = await fetch(`/v1/${path}`, {headers: {Authorization: `Bearer ${token}`},
      credentials: "omit", cache: "no-store", redirect: "error", signal: abort.signal});
    if (!response.ok) throw new Error(`Runtime HTTP ${response.status}`);
    return response.json();
  }
  function validDetail(value: Detail, scope: string) {
    if (value.scope_id !== scope || value.binding.agent_id !== agent || value.source !== "isolated-synthetic" || value.publish_eligible !== false)
      throw new Error("历史详情范围不匹配");
  }
  try {
    const identity = await get("workbench/identity");
    if (current !== generation) return;
    if (identity.auth_method !== "user" || typeof identity.scope_id !== "string") throw new Error("验签用户身份不匹配");
    const query = new URLSearchParams({agent_id: agent});
    let path = "isolated-evaluations/history";
    if (kind === "list") {
      query.set("limit", "20"); if (cursor) query.set("before", cursor); if (status.value) query.set("status", status.value);
    } else if (kind === "detail") path += `/${encodeURIComponent(id!)}`;
    else {path += "/compare"; query.set("left", chosen[0]); query.set("right", chosen[1]);}
    const value = await get(`${path}?${query}`);
    if (current !== generation) return;
    if (value.scope_id !== identity.scope_id) throw new Error("历史身份不匹配");
    if (kind === "list") {
      if (value.agent_id !== agent || !Array.isArray(value.items) || value.publish_eligible !== false) throw new Error("历史列表范围不匹配");
      items.value = value.items; next.value = value.next_cursor;
      message.value = items.value.length ? "已读取本人历史；翻页不保证历史状态快照。" : "当前筛选没有本人历史记录。";
    } else if (kind === "detail") {validDetail(value, identity.scope_id); details.value = [value];}
    else {
      validDetail(value.left, identity.scope_id); validDetail(value.right, identity.scope_id);
      details.value = [value.left, value.right]; comparison.value = value.comparison;
    }
  } catch (error) {
    if (current === generation) {clear(); message.value = `${error}；历史读取失败或权限已撤销。未决恢复与本人ACK请使用上方账本。`;}
  } finally {if (current === generation) busy.value = false;}
}
function select(id: string) {
  comparison.value = null; details.value = [];
  selected.value = selected.value.includes(id) ? selected.value.filter(value => value !== id) : [...selected.value, id].slice(-2);
}
function rate(value: number | null | undefined) {return value == null ? "未知（证据不完整）" : `${value * 100}%`;}
</script>
<template>
  <section class="panel" aria-label="本人评测历史与版本对照">
    <h3>本人评测历史与版本对照</h3>
    <p>每次读取核验当前Agent管理权限。撤权后不可浏览历史；未决恢复与本人ACK仍使用上方账本。仅本机诊断，不取得发布资格。</p>
    <label>历史执行状态 <select v-model="status" :disabled="busy"><option value="">全部状态</option>
      <option v-for="value in ['PREPARED','EXECUTING','COMMITTED','REJECTED_NO_EFFECT']" :key="value" :value="value">{{value}}</option>
    </select></label>
    <button :disabled="busy" @click="read('list')">读取本人评测历史</button>
    <button :disabled="busy || !next" @click="read('list', next!)">下一页评测历史</button>
    <p role="status">{{message}}</p>
    <ul aria-label="本人评测历史列表">
      <li v-for="item in items" :key="item.attempt_id">
        <label><input type="checkbox" :checked="selected.includes(item.attempt_id)" :disabled="busy" @change="select(item.attempt_id)" />对照 {{item.attempt_id}}</label>
        <span>{{item.created_at}} · Runtime {{item.agent_version}} · 数据集 {{item.dataset_version}} · {{item.status}} · {{item.acknowledged ? '已ACK' : '未ACK'}}</span>
        <button :disabled="busy" @click="read('detail', undefined, item.attempt_id)">查看评测明细</button>
      </li>
    </ul>
    <p>选择两条记录，按选择顺序比较后者减前者；只比较同Agent、冻结数据集、场景与判定条件一致的完整结果。</p>
    <button :disabled="busy || selected.length !== 2" @click="read('compare')">只读对照选中评测</button>
    <section v-if="comparison" aria-label="评测版本对照结果">
      <p>{{comparison.comparable ? '条件一致，可对照' : '不可比'}}</p>
      <p>通过率差值：{{comparison.pass_rate_delta == null ? '未知' : `${comparison.pass_rate_delta * 100} 个百分点`}}</p>
      <p>排除原因：{{comparison.reasons.join('、') || '无'}}。差值不代表真实模型质量提升或发布资格。</p>
    </section>
    <section v-for="detail in details" :key="detail.attempt_id" aria-label="历史评测明细">
      <h4>{{detail.attempt_id}}</h4>
      <p>Runtime {{detail.binding.agent_version}} · 数据集 {{detail.binding.dataset_version}} · {{detail.status}} · {{detail.acknowledged ? '已ACK' : '未ACK'}}</p>
      <p>选中 / 已验证 / 通过：{{detail.receipt?.result.selected_count ?? '未知'}} / {{detail.receipt?.result.verified_count ?? '未知'}} / {{detail.receipt?.result.passed_count ?? '未知'}}</p>
      <p>通过率：{{rate(detail.receipt?.result.pass_rate)}}</p>
      <ul><li v-for="child in detail.children" :key="child.case_id">{{child.case_id}} · {{child.run_id}} · {{child.evidence?.judgement || '未决'}} · {{child.evidence?.reasons.join('、')}}</li></ul>
      <p>不展示提示词、工具正文或助手输出；历史查询不会执行、补跑或ACK。</p>
    </section>
  </section>
</template>
