<!-- Skill 审核（S9）：上架状态机 submitted → scanned → evaluated → approved → listed（→ deprecated / revoked；scan_failed、eval_failed 为终态）。
     列表来自 Console API（转发能力网关）；审批 = POST /skills/{n}/versions/{v}/approve（Console 以服务令牌 + 审批人用户令牌转发网关，
     Cedar approve_skill + 禁自批）；吊销 = POST /skills/{n}/versions/{v}/revoke（即时生效，节点随心跳拉取吊销列表）。
     开发态以 X-Console-* 头表示操作者（与 Agent 页共用）；审批人用户令牌只保存在内存，不落浏览器存储。 -->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";

/** 扫描发现项（字段名兼容 rule / rule_id、path / file）。 */
interface Finding { rule?: string; rule_id?: string; severity?: string; path?: string; file?: string; line?: number; message?: string }
/** 网关 GET /skills/{n}/versions/{v} 的视图（契约 ②）；列表接口逐项同形。 */
interface SkillVersion {
  name: string;
  version: string;
  tenant?: string;
  status: string;
  manifest_sha256?: string | null;
  scan_report?: { passed?: boolean; rules_version?: string; findings?: Finding[] } | null;
  eval?: { run_ref?: string | null; pass_rate?: number | null; items?: unknown[] } | null;
  submitted_by?: string | null;
  reviewed_by?: string | null;
  listed_at?: string | null;
  revoked_at?: string | null;
}

const STATUSES = ["", "submitted", "scanned", "scan_failed", "evaluated", "eval_failed", "approved", "listed", "deprecated", "revoked"] as const;
const ROLES = ["USER", "ADMIN", "TENANT_ADMIN", "PLATFORM_ADMIN"] as const;
const saved = (() => { try { return JSON.parse(localStorage.getItem("console.operator") ?? "null"); } catch { return null; } })();
const op = reactive<{ user: string; role: string; tenant: string }>(saved ?? { user: "ops", role: "TENANT_ADMIN", tenant: "default" });
const reviewerToken = ref(""); // 审批人（skill-reviewer）访问令牌，放 X-AI-User-Token
const H = computed<Record<string, string>>(() => ({
  "content-type": "application/json",
  "X-Console-User": op.user,
  "X-Console-Role": op.role,
  "X-Console-Tenant": op.tenant,
  ...(reviewerToken.value.trim() ? { "X-AI-User-Token": reviewerToken.value.trim() } : {}),
}));

const list = ref<SkillVersion[]>([]);
const status = ref("");
const lookup = reactive({ name: "", version: "" });
const open = ref<string | null>(null); // 展开扫描 / 评测详情的行
const msg = ref("");
const writing = ref(false);
const canRevoke = computed(() => ["TENANT_ADMIN", "PLATFORM_ADMIN"].includes(op.role));
let generation = 0;
function invalidate() { generation += 1; list.value = []; open.value = null; msg.value = ""; }
const key = (s: SkillVersion) => `${s.name}@${s.version}`;
const findingsOf = (s: SkillVersion) => s.scan_report?.findings ?? [];

/** 调用 Console API，失败时显示状态码与原因（403 自批 / 无审批角色、409 状态不符等）。 */
async function call<T>(method: string, path: string, body: unknown, current: number): Promise<T | null> {
  if (current !== generation) return null;
  try {
    const res = await fetch(`/console${path}`, { method, cache: "no-store", redirect: "error", headers: { ...H.value }, body: body === undefined ? undefined : JSON.stringify(body) });
    const text = await res.text();
    if (current !== generation) return null;
    msg.value = `${method} ${path} → ${res.status}${res.ok ? "" : "：请求被拒绝或数据不可用；未展示服务端正文。"}`;
    if (!res.ok) {
      if ([401, 403, 404].includes(res.status)) { list.value = []; open.value = null; }
      return null;
    }
    return (text ? JSON.parse(text) : {}) as T;
  } catch {
    if (current === generation) msg.value = `请求失败：${method === "GET" ? "数据不可用" : "变更结果可能未知，请核对服务端状态"}；未自动重试。`;
    return null;
  }
}

async function load() {
  if (writing.value) return;
  invalidate();
  const current = generation;
  const q = status.value ? `?status=${encodeURIComponent(status.value)}` : "";
  const result = await call<SkillVersion[]>("GET", `/skills${q}`, undefined, current);
  if (current === generation) list.value = Array.isArray(result) ? result : [];
}

/** 按名称与版本查询单个版本（列表接口不可用或需刷新单行时）。 */
async function find() {
  if (writing.value) return;
  const current = ++generation;
  const v = await call<SkillVersion>("GET", `/skills/${encodeURIComponent(lookup.name)}/versions/${encodeURIComponent(lookup.version)}`, undefined, current);
  if (v && current === generation) list.value = [v, ...list.value.filter((s) => key(s) !== key(v))];
}

async function refresh(s: SkillVersion, current: number) {
  const keep = msg.value;
  const v = await call<SkillVersion>("GET", `/skills/${encodeURIComponent(s.name)}/versions/${encodeURIComponent(s.version)}`, undefined, current);
  if (current !== generation) return;
  if (v) list.value = list.value.map((x) => (key(x) === key(v) ? v : x));
  if (v) msg.value = keep;
}

async function approve(s: SkillVersion) {
  if (writing.value || !reviewerToken.value.trim()) return;
  if (!confirm(`审批上架 ${key(s)}？通过后由网关签名并置为 listed（提交人不能自批）。`)) return;
  const current = ++generation;
  writing.value = true;
  try {
    const ok = await call("POST", `/skills/${encodeURIComponent(s.name)}/versions/${encodeURIComponent(s.version)}/approve`, {}, current);
    if (ok && current === generation) await refresh(s, current);
  } finally { if (current === generation) writing.value = false; }
}

async function revoke(s: SkillVersion) {
  if (writing.value || !canRevoke.value || !reviewerToken.value.trim()) return;
  const reason = prompt(`吊销 ${key(s)}（立即生效，已绑定该版本的 Agent 运行将以 SKILL_REVOKED 失败）。原因：`, "");
  if (reason === null) return;
  if (!reason.trim() || reason.length > 500) { msg.value = "吊销原因须为非空文本且不超过 500 字符。"; return; }
  const current = ++generation;
  writing.value = true;
  try {
    const ok = await call("POST", `/skills/${encodeURIComponent(s.name)}/versions/${encodeURIComponent(s.version)}/revoke`, { reason }, current);
    if (ok && current === generation) await refresh(s, current);
  } finally { if (current === generation) writing.value = false; }
}

const pct = (r?: number | null) => (r === null || r === undefined ? "—" : `${Math.round(r * 1000) / 10}%`);

onMounted(load);
watch(op, () => {
  invalidate(); reviewerToken.value = ""; lookup.name = lookup.version = "";
  try { localStorage.setItem("console.operator", JSON.stringify(op)); } catch {}
  void load();
}, { flush: "sync" });
watch(reviewerToken, () => { invalidate(); msg.value = "审批人令牌已改变，请手动刷新后操作。"; }, { flush: "sync" });
onBeforeUnmount(() => { invalidate(); reviewerToken.value = ""; });
</script>
<template>
  <h2>Skill 审核</h2>
  <fieldset :disabled="writing" style="max-width:720px;margin-bottom:12px">
    <legend>当前操作者（开发态身份头）</legend>
    用户 <input v-model.lazy="op.user" style="width:90px" />
    角色 <select v-model="op.role"><option v-for="r in ROLES" :key="r">{{ r }}</option></select>
    租户 <input v-model.lazy="op.tenant" style="width:90px" />
    <div style="margin-top:6px">
      审批人令牌 <input v-model="reviewerToken" type="password" autocomplete="off" placeholder="skill-reviewer 用户访问令牌（X-AI-User-Token，仅内存）" style="width:420px" />
    </div>
  </fieldset>
  <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px">
    状态 <select v-model="status" :disabled="writing" @change="load"><option v-for="s in STATUSES" :key="s" :value="s">{{ s || "全部" }}</option></select>
    <button :disabled="writing" @click="load">刷新</button>
    <form @submit.prevent="find" style="display:flex;gap:6px">
      <input v-model="lookup.name" :disabled="writing" placeholder="Skill 名称" required style="width:140px" />
      <input v-model="lookup.version" :disabled="writing" placeholder="版本" required style="width:80px" />
      <button :disabled="writing">查询</button>
    </form>
  </div>
  <p role="status" style="font-size:12px;color:#646a73">{{ msg }}</p>
  <p>审批权限与禁自批由网关复核；吊销另需租户管理员。令牌改变后请刷新，不会修改真实账号或角色。</p>
  <table style="border-collapse:collapse" border="1" cellpadding="6">
      <thead><tr><th>Skill</th><th>状态</th><th>扫描</th><th>评测</th><th>提交人</th><th>审批人</th><th>操作</th></tr></thead><tbody>
    <template v-for="s in list" :key="key(s)">
      <tr>
        <td>{{ s.name }}@{{ s.version }}<div v-if="s.manifest_sha256" style="font-size:11px;color:#8f959e">{{ s.manifest_sha256.slice(0, 12) }}</div></td>
        <td><b :style="{ color: ['scan_failed', 'eval_failed', 'revoked'].includes(s.status) ? '#d83931' : s.status === 'listed' ? '#2ea121' : '#1f2329' }">{{ s.status }}</b></td>
        <td>
          <template v-if="s.scan_report">{{ s.scan_report.passed ? "通过" : "未通过" }} · {{ findingsOf(s).length }} 项 · 规则 {{ s.scan_report.rules_version ?? "—" }}</template>
          <template v-else>—</template>
        </td>
        <td>
          <template v-if="s.eval">{{ pct(s.eval.pass_rate) }}<span v-if="s.eval.items"> · {{ s.eval.items.length }} 条</span></template>
          <template v-else>—</template>
        </td>
        <td>{{ s.submitted_by ?? "—" }}</td>
        <td>{{ s.reviewed_by ?? "—" }}</td>
        <td>
          <button :disabled="writing" @click="open = open === key(s) ? null : key(s)">{{ open === key(s) ? "收起" : "详情" }}</button>
          <button v-if="s.status === 'evaluated'" :disabled="writing || !reviewerToken.trim()" @click="approve(s)">审批通过</button>
          <button v-if="['approved', 'listed', 'deprecated'].includes(s.status)" :disabled="writing || !canRevoke || !reviewerToken.trim()" @click="revoke(s)">吊销</button>
        </td>
      </tr>
      <tr v-if="open === key(s)">
        <td colspan="7" style="background:#f7f8fa">
          <div v-if="findingsOf(s).length">
            <b>扫描发现</b>
            <ul style="margin:4px 0">
              <li v-for="(f, i) in findingsOf(s)" :key="i">[{{ f.severity ?? "?" }}] {{ f.rule ?? f.rule_id }} · {{ f.path ?? f.file ?? "" }}<span v-if="f.line">:{{ f.line }}</span> — {{ f.message }}</li>
            </ul>
          </div>
          <div v-if="s.eval"><b>评测</b> run_ref：{{ s.eval.run_ref ?? "—" }}，通过率 {{ pct(s.eval.pass_rate) }}</div>
          <div v-if="s.listed_at || s.revoked_at" style="font-size:12px">上架 {{ s.listed_at ?? "—" }} · 吊销 {{ s.revoked_at ?? "—" }}</div>
        </td>
      </tr>
    </template></tbody>
  </table>
</template>
