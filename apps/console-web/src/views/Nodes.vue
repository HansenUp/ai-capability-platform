<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { nodeReceiptGuidance } from "../node-diagnostics";
import { commandPath, readSession } from "../operations";
import { CommandSubmissionError, prepareCommand, submitCommand, type CommandForm, type PreparedCommand } from "../node-commands";
import { auditPath, candidateId, historyPath, matchesAttempt, nextCursor, validateCommandSummary, validateHistoryPage, type AuditCandidate, type CommandSummary } from "../command-history";

interface NodeView {
  node_id: string; topology: string; labels: string[]; version: string;
  last_seen: string; load: number; qps: number; healthy: boolean; ready?: boolean;
}
interface GroupView { name: string; description: string; created_at: string; nodes: string[] }
interface Routing { groups: Record<string, string[]>; bindings: Record<string, Record<string, number>>; default_group: string }
interface Operator { user: string; role: string; tenant: string }

function savedOperator(): Operator {
  try {
    const saved = JSON.parse(localStorage.getItem("console.operator") ?? "null");
    if (saved && [saved.user, saved.role, saved.tenant].every(value => typeof value === "string")) return saved;
  } catch {}
  return { user: "ops", role: "TENANT_ADMIN", tenant: "default" };
}
const operator = reactive(savedOperator());
const roles = ["USER", "ADMIN", "TENANT_ADMIN", "PLATFORM_ADMIN"];
const platformAdmin = computed(() => operator.role === "PLATFORM_ADMIN");
const tenantAdmin = computed(() => ["TENANT_ADMIN", "PLATFORM_ADMIN"].includes(operator.role));
const nodes = ref<NodeView[]>([]);
const groups = ref<GroupView[]>([]);
const routing = ref<Routing>({ groups: {}, bindings: {}, default_group: "general" });
const busy = ref(false);
const error = ref("");
const message = ref("");
const groupForm = reactive({ name: "", description: "" });
const editingGroup = ref<string | null>(null);
const editingNode = ref<string | null>(null);
const nodeLabels = ref<string[]>([]);
const agentId = ref("");
const weights = ref<Record<string, number>>(Object.create(null));
const pendingDelete = ref<string | null>(null);
const confirmBinding = ref(false);
const commandForm = reactive<CommandForm>({ node: "", type: "reload_config", pollInterval: "", timeout: "", restartTimeout: "30",
  agent: "", base: "", expected: "", target: "", alias: "", skills: [{ name: "", version: "", sha: "" }] });
const commandPreview = ref<PreparedCommand | null>(null);
const commandBusy = ref(false);
const commandError = ref("");
const commandUnknown = ref(false);
const unknownAttempt = ref<{ prepared: PreparedCommand; since: string; until: string } | null>(null);
const submittedCommand = ref<{ node: string; id: string; tenant: string } | null>(null);
watch([operator, commandForm], () => { commandPreview.value = null; }, { flush: "sync" });
watch(operator, () => { submittedCommand.value = null; }, { flush: "sync" });
function reviewCommand() {
  commandError.value = ""; commandPreview.value = null;
  try { commandPreview.value = prepareCommand(commandForm, operator, nodes.value); }
  catch (failure) { commandError.value = String(failure); }
}
async function sendCommand() {
  if (!commandPreview.value || commandBusy.value || commandUnknown.value) return;
  const prepared = JSON.parse(JSON.stringify(commandPreview.value)) as PreparedCommand;
  try {
    if (JSON.stringify(prepared) !== JSON.stringify(prepareCommand(commandForm, operator, nodes.value))) {
      commandPreview.value = null; return;
    }
  } catch (failure) { commandPreview.value = null; commandError.value = String(failure); return; }
  commandBusy.value = true; commandPreview.value = null; commandError.value = ""; submittedCommand.value = null;
  const startedAt = new Date(Date.now() - 60000).toISOString();
  try {
    const id = await submitCommand(prepared, import.meta.env.DEV);
    submittedCommand.value = { node: prepared.node, id, tenant: prepared.operator.tenant };
    receiptQuery.node = prepared.node; receiptQuery.command = id;
    await readReceipt();
  } catch (failure) {
    commandError.value = String(failure);
    commandUnknown.value = failure instanceof CommandSubmissionError && failure.outcomeUnknown;
    if (commandUnknown.value) {
      unknownAttempt.value = { prepared, since: startedAt, until: new Date(Date.now() + 60000).toISOString() };
      historyQuery.node = prepared.node; auditQuery.node = prepared.node; auditQuery.actor = prepared.operator.user;
      auditQuery.since = unknownAttempt.value.since; auditQuery.until = unknownAttempt.value.until;
    }
  } finally { commandBusy.value = false; }
}
interface CommandReceipt { id: string; node_id: string; tenant_id: string; type: string; status: string; created_at: string | null; acked_at: string | null }
const receiptQuery = reactive({ node: "", command: "" });
const receipt = ref<CommandReceipt | null>(null);
const receiptError = ref("");
const receiptBusy = ref(false);
const receiptReader = readSession(import.meta.env.DEV);
let receiptGeneration = 0;
function clearReceipt() {
  receiptGeneration += 1; receiptReader.invalidate(); receipt.value = null; receiptError.value = ""; receiptBusy.value = false;
}
watch([operator, receiptQuery], clearReceipt, { flush: "sync" });
async function readReceipt() {
  clearReceipt();
  const current = receiptGeneration;
  receiptBusy.value = true;
  try {
    const result = await receiptReader.read<CommandReceipt>(commandPath(receiptQuery.node, receiptQuery.command), { ...operator });
    if (current !== receiptGeneration) return;
    validateCommandSummary(result, operator.tenant, receiptQuery.node.trim(), receiptQuery.command.trim());
    receipt.value = result;
  } catch (failure) { if (current === receiptGeneration) receiptError.value = String(failure); }
  finally { if (current === receiptGeneration) receiptBusy.value = false; }
}
const historyQuery = reactive({ node: "", status: "", type: "" });
const auditQuery = reactive({ node: "", actor: "", since: "", until: "" });
const historyRows = ref<CommandSummary[]>([]), auditRows = ref<AuditCandidate[]>([]);
const historyCursor = ref<number | null>(null), auditCursor = ref<number | null>(null);
const historyBusy = ref(false), auditBusy = ref(false);
const historyError = ref(""), auditError = ref("");
const historyLoaded = ref(false), auditLoaded = ref(false);
const historyReader = readSession(import.meta.env.DEV), auditReader = readSession(import.meta.env.DEV);
let historyGeneration = 0, auditGeneration = 0;
function clearHistory() {
  historyGeneration++; historyReader.invalidate(); historyRows.value = []; historyCursor.value = null;
  historyBusy.value = false; historyError.value = ""; historyLoaded.value = false;
}
function clearAudit() {
  auditGeneration++; auditReader.invalidate(); auditRows.value = []; auditCursor.value = null;
  auditBusy.value = false; auditError.value = ""; auditLoaded.value = false;
}
watch([operator, historyQuery], clearHistory, { flush: "sync" });
watch([operator, auditQuery], clearAudit, { flush: "sync" });
async function loadHistory(more = false) {
  if (historyBusy.value || (more && historyCursor.value === null)) return;
  if (!more) clearHistory();
  const current = historyGeneration, previous = more ? historyCursor.value : null;
  historyBusy.value = true; historyError.value = "";
  try {
    const page = await historyReader.read<{ tenant_id: string; node_id: string; items: CommandSummary[]; next_before_id: number | null; order: string }>(
      historyPath(historyQuery.node, historyQuery.status, historyQuery.type, previous), { ...operator });
    if (current !== historyGeneration) return;
    const next = validateHistoryPage(page, operator.tenant, historyQuery.node.trim(), historyQuery.status, historyQuery.type,
      previous, more ? historyRows.value : []);
    historyRows.value = more ? [...historyRows.value, ...page.items] : page.items;
    historyCursor.value = next; historyLoaded.value = true;
  } catch (failure) { if (current === historyGeneration) { clearHistory(); historyError.value = String(failure); } }
  finally { if (current === historyGeneration) historyBusy.value = false; }
}
async function loadAudit(more = false) {
  if (auditBusy.value || (more && auditCursor.value === null)) return;
  if (!more) clearAudit();
  const current = auditGeneration, previous = more ? auditCursor.value : null;
  auditBusy.value = true; auditError.value = "";
  try {
    const page = await auditReader.read<{ items: AuditCandidate[]; next_before_id: number | null; order: string }>(
      auditPath(auditQuery.node, auditQuery.actor, auditQuery.since, auditQuery.until, previous), { ...operator });
    if (current !== auditGeneration) return;
    if (page.order !== "newest_first" || !Array.isArray(page.items)) throw new Error("审计页格式不匹配。");
    for (const row of page.items) candidateId(row, operator.tenant, auditQuery.node.trim(), auditQuery.actor.trim());
    const next = nextCursor(page.next_before_id, previous);
    auditRows.value = more ? [...auditRows.value, ...page.items] : page.items;
    auditCursor.value = next; auditLoaded.value = true;
  } catch (failure) { if (current === auditGeneration) { clearAudit(); auditError.value = String(failure); } }
  finally { if (current === auditGeneration) auditBusy.value = false; }
}
function locateUnknown() {
  const attempt = unknownAttempt.value;
  if (!attempt || operator.tenant !== attempt.prepared.operator.tenant) return;
  auditQuery.node = attempt.prepared.node; auditQuery.actor = attempt.prepared.operator.user;
  auditQuery.since = attempt.since; auditQuery.until = attempt.until;
  void loadAudit();
}
async function openReceipt(node: string, id: string) {
  receiptQuery.node = node; receiptQuery.command = id; await readReceipt();
}
onBeforeUnmount(() => { receiptReader.invalidate(); historyReader.invalidate(); auditReader.invalidate(); });

async function api<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(`/console${path}`, {
    method,
    headers: { "Content-Type": "application/json", "X-Console-User": operator.user,
      "X-Console-Role": operator.role, "X-Console-Tenant": operator.tenant },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  if (!response.ok) throw new Error(`HTTP ${response.status}：${text}`);
  return text ? JSON.parse(text) as T : {} as T;
}
async function readAll() {
  const [nextNodes, nextGroups, nextRouting] = await Promise.all([
    api<NodeView[]>("GET", "/nodes"), api<GroupView[]>("GET", "/node-groups/details"), api<Routing>("GET", "/routing"),
  ]);
  nodes.value = nextNodes;
  groups.value = nextGroups;
  routing.value = nextRouting;
}
async function run(action?: () => Promise<void>) {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  message.value = "";
  let saved = false;
  try {
    if (action) { await action(); saved = true; }
    await readAll();
    message.value = saved ? "已保存，列表与路由已刷新。" : "已刷新。";
  } catch (failure) {
    error.value = `${saved ? "变更已保存，但刷新失败，请重新刷新。" : "操作失败："}${failure instanceof Error ? failure.message : String(failure)}`;
  } finally { busy.value = false; }
}
function editGroup(group: GroupView) {
  editingGroup.value = group.name;
  groupForm.name = group.name;
  groupForm.description = group.description;
}
function clearGroup() { editingGroup.value = null; groupForm.name = ""; groupForm.description = ""; }
async function saveGroup() {
  await run(async () => {
    if (editingGroup.value) await api("PUT", `/node-groups/${encodeURIComponent(editingGroup.value)}`, { description: groupForm.description });
    else await api("POST", "/node-groups", { name: groupForm.name.trim(), description: groupForm.description });
    clearGroup();
  });
}
async function deleteGroup(group: GroupView) {
  await run(async () => {
    await api("DELETE", `/node-groups/${encodeURIComponent(group.name)}`);
    pendingDelete.value = null;
    if (editingGroup.value === group.name) clearGroup();
  });
}
function editNode(node: NodeView) { editingNode.value = node.node_id; nodeLabels.value = [...node.labels]; }
async function saveNode() {
  await run(async () => {
    await api("PUT", `/nodes/${encodeURIComponent(editingNode.value!)}/groups`, { labels: nodeLabels.value });
    editingNode.value = null;
  });
}
function loadBinding() { weights.value = Object.assign(Object.create(null), routing.value.bindings[agentId.value.trim()] ?? {}); }
function hasWeight(name: string) { return Object.keys(weights.value).includes(name); }
function onlineCount(name: string) {
  return Object.prototype.hasOwnProperty.call(routing.value.groups, name) ? routing.value.groups[name].length : 0;
}
function toggleBinding(name: string, checked: boolean) {
  if (checked) weights.value[name] = 100;
  else delete weights.value[name];
}
async function saveBindings() {
  await run(async () => {
    await api("PUT", `/agents/${encodeURIComponent(agentId.value.trim())}/bindings`,
      Object.entries(weights.value).map(([node_group, weight]) => ({ node_group, weight })));
    confirmBinding.value = false;
  });
}
watch(operator, () => {
  try { localStorage.setItem("console.operator", JSON.stringify(operator)); } catch {}
  nodes.value = []; groups.value = [];
  routing.value = { groups: {}, bindings: {}, default_group: "general" };
  agentId.value = ""; weights.value = Object.create(null); editingNode.value = null; clearGroup();
  pendingDelete.value = null; confirmBinding.value = false;
  void run();
});
onMounted(() => run());
</script>

<template>
  <section class="nodes-page">
    <header><div><h2>节点分组与路由</h2><RouterLink to="/manage/node-control">独立排空／恢复隔离实验</RouterLink><p>仅本机开发管理。分组属于共享基础设施；Agent 路由绑定按租户隔离。</p></div>
      <button :disabled="busy" @click="run()">{{ busy ? "处理中…" : "刷新" }}</button></header>
    <fieldset :disabled="busy || commandBusy" class="operator">
      <legend>当前操作者（开发态身份头，不代表生产鉴权）</legend>
      <label>用户 <input v-model.lazy="operator.user" /></label>
      <label>角色 <select v-model="operator.role"><option v-for="role in roles" :key="role">{{ role }}</option></select></label>
      <label>租户 <input v-model.lazy="operator.tenant" /></label>
    </fieldset>
    <p v-if="!platformAdmin" class="hint">分组增删改与节点成员调整仅限 PLATFORM_ADMIN；租户管理员可查看并配置本租户 Agent 绑定。</p>
    <p v-if="error" role="alert" class="error">{{ error }}</p><p v-if="message" role="status">{{ message }}</p>
    <section aria-labelledby="groups-heading">
      <h3 id="groups-heading">节点分组</h3>
      <form v-if="platformAdmin" @submit.prevent="saveGroup"><fieldset :disabled="busy">
        <legend>{{ editingGroup ? `编辑 ${editingGroup}` : "创建分组" }}</legend>
        <label>分组名称 <input v-model="groupForm.name" :readonly="editingGroup !== null" required maxlength="64" pattern="[A-Za-z0-9][A-Za-z0-9_.-]{0,63}" placeholder="例如 aftersales" /></label>
        <label>说明 <input v-model="groupForm.description" maxlength="500" /></label>
        <button>{{ editingGroup ? "保存说明" : "创建分组" }}</button><button v-if="editingGroup" type="button" @click="clearGroup">取消编辑</button>
      </fieldset></form>
      <table><thead><tr><th>分组</th><th>说明</th><th>成员 / 在线</th><th>操作</th></tr></thead><tbody>
        <tr v-for="group in groups" :key="group.name"><td>{{ group.name }} <small v-if="group.name === 'general'">默认组</small></td>
          <td>{{ group.description || "—" }}</td><td>{{ group.nodes.length }} / {{ onlineCount(group.name) }}<div><small>{{ group.nodes.join("、") || "空组" }}</small></div></td>
          <td><button :disabled="busy || !platformAdmin" @click="editGroup(group)" :aria-label="`编辑分组 ${group.name}`">编辑</button>
            <button :disabled="busy || !platformAdmin || group.name === 'general' || group.nodes.length > 0" @click="pendingDelete = group.name" :aria-label="`删除分组 ${group.name}`">删除</button>
            <div v-if="pendingDelete === group.name && platformAdmin" role="group" :aria-label="`确认删除 ${group.name}`">
              <p>确认删除？存在节点或任意租户绑定时服务器将拒绝。</p>
              <button :disabled="busy" @click="deleteGroup(group)">确认删除</button><button :disabled="busy" @click="pendingDelete = null">取消删除</button>
            </div></td></tr>
        <tr v-if="!groups.length"><td colspan="4">暂无可显示的分组。</td></tr>
      </tbody></table>
    </section>
    <section aria-labelledby="nodes-heading">
      <h3 id="nodes-heading">节点成员管理</h3>
      <table><thead><tr><th>节点 / 版本</th><th>状态</th><th>分组</th><th>最近心跳</th><th>负载 / QPS</th><th>操作</th></tr></thead><tbody>
        <tr v-for="node in nodes" :key="node.node_id"><td>{{ node.node_id }}<div><small>{{ node.topology }} · {{ node.version }}</small></div></td>
          <td :class="node.healthy ? 'healthy' : 'offline'">{{ node.ready === false ? "排空中" : node.healthy ? "在线" : "心跳超时" }}</td><td>{{ node.labels.join("、") || "未分组" }}</td>
          <td>{{ new Date(node.last_seen).toLocaleString() }}</td><td>{{ node.load }} / {{ node.qps }}</td>
          <td><button :disabled="busy || !platformAdmin" @click="editNode(node)" :aria-label="`调整节点 ${node.node_id} 分组`">调整分组</button></td></tr>
        <tr v-if="!nodes.length"><td colspan="6">暂无注册节点。</td></tr>
      </tbody></table>
      <form v-if="editingNode && platformAdmin" @submit.prevent="saveNode"><fieldset :disabled="busy"><legend>调整 {{ editingNode }} 的分组（至少一个）</legend>
        <label v-for="group in groups" :key="group.name"><input v-model="nodeLabels" type="checkbox" :value="group.name" />{{ group.name }}</label>
        <button :disabled="!nodeLabels.length">保存节点分组</button><button type="button" @click="editingNode = null">取消</button>
      </fieldset></form>
    </section>
    <section aria-labelledby="command-heading">
      <h3 id="command-heading">下发节点指令</h3>
      <p>仅本机开发；202 只表示入队，不表示已执行。节点仍按 S10 契约校验并回执；页面不会绕过签名、模型版本 CAS 或排空门禁。</p>
      <p v-if="commandUnknown" role="alert">上次下发结果未知，本页已锁定再次下发。先核对原租户节点的 node.command 审计和回执，不可通过刷新页面盲目重试。</p>
      <p v-if="unknownAttempt">待对账上下文：{{ unknownAttempt.prepared.operator.tenant }} / {{ unknownAttempt.prepared.node }} / {{ unknownAttempt.prepared.operator.user }} / {{ unknownAttempt.prepared.body.type }}。
        <button :disabled="!tenantAdmin || operator.tenant !== unknownAttempt.prepared.operator.tenant || auditBusy" @click="locateUnknown">只读定位未知下发审计</button>
        <span v-if="operator.tenant !== unknownAttempt.prepared.operator.tenant">请切回原租户；此入口不跨租户读取。</span></p>
      <form @submit.prevent="reviewCommand"><fieldset :disabled="!tenantAdmin || busy || commandBusy || commandUnknown">
        <legend>固定指令类型与结构化参数</legend>
        <label>目标节点 <select v-model="commandForm.node" required><option value="">请选择已注册节点</option>
          <option v-for="node in nodes" :key="node.node_id" :value="node.node_id">{{ node.node_id }} · {{ node.healthy ? '在线' : '离线或排空' }}</option></select></label>
        <label>指令类型 <select v-model="commandForm.type"><option value="reload_config">reload_config</option><option value="reload_skills">reload_skills</option>
          <option value="switch_model">switch_model</option><option value="graceful_restart" :disabled="!platformAdmin">graceful_restart（平台管理员）</option></select></label>
        <div v-if="commandForm.type === 'reload_config'">
          <p>仅调整 NodeAgent 当前进程的轮询与超时，不修改 Runtime 安全配置或持久化配置。</p>
          <label>轮询秒数 <input v-model="commandForm.pollInterval" type="number" min="1" max="3600" step="any" /></label>
          <label>请求超时秒数 <input v-model="commandForm.timeout" type="number" min="0.1" max="120" step="any" /></label>
        </div>
        <div v-if="commandForm.type === 'switch_model'">
          <p>只影响后续未固定版本的新 Run；同租户已发布全量目标、非模型配置一致和 expected_version CAS 均由节点复核。</p>
          <label>Agent ID <input v-model="commandForm.agent" required maxlength="64" /></label>
          <label>基线版本 <input v-model="commandForm.base" required maxlength="32" /></label>
          <label>预期当前版本 <input v-model="commandForm.expected" required maxlength="32" /></label>
          <label>目标版本 <input v-model="commandForm.target" required maxlength="32" /></label>
          <label>目标模型别名 <input v-model="commandForm.alias" required maxlength="64" /></label>
        </div>
        <div v-if="commandForm.type === 'reload_skills'">
          <p>仅提供已签名清单的名称／版本／SHA256；不接受 URL、路径或脚本，不绕过下载后的验签与原子物化。</p>
          <div v-for="(skill, index) in commandForm.skills" :key="index">
            <label>Skill 名称 <input v-model="skill.name" required /></label><label>Skill 版本 <input v-model="skill.version" required /></label>
            <label>清单 SHA256 <input v-model="skill.sha" required minlength="64" maxlength="64" /></label>
            <button type="button" @click="commandForm.skills.splice(index, 1)">移除此项</button>
          </div><button type="button" @click="commandForm.skills.push({ name: '', version: '', sha: '' })">添加 Skill</button>
        </div>
        <div v-if="commandForm.type === 'graceful_restart'">
          <p>共享节点排空需要 PLATFORM_ADMIN；不强杀、不提前释放租约。ok 仅确认排空与退出请求，完成仍需新 PID／boot_id／ready。</p>
          <label>排空超时秒数 <input v-model="commandForm.restartTimeout" type="number" min="0.1" max="300" step="any" required /></label>
        </div><button>校验并预览指令</button>
      </fieldset></form>
      <div v-if="commandPreview" role="group" aria-label="确认下发节点指令">
        <p>请核对操作者 {{ commandPreview.operator.user }}／{{ commandPreview.operator.role }}、租户 {{ commandPreview.operator.tenant }}、节点 {{ commandPreview.node }}。离线或排空节点可能延迟或拒绝执行。</p>
        <pre>{{ JSON.stringify(commandPreview.body, null, 2) }}</pre>
        <button :disabled="commandBusy || commandUnknown" @click="sendCommand">确认下发一次</button><button @click="commandPreview = null">取消下发</button>
      </div>
      <p v-if="commandError" role="alert">{{ commandError }}</p>
      <p v-if="submittedCommand" role="status">已入队：{{ submittedCommand.tenant }} / {{ submittedCommand.node }} / {{ submittedCommand.id }}。不自动重发；下方手动刷新回执。</p>
    </section>
    <section aria-labelledby="history-heading">
      <h3 id="history-heading">节点指令历史 · 只读分页</h3>
      <p>仅当前租户，按记录 ID 倒序；状态会变化，不保证跨页快照。查询不会拉取节点待办、ACK 或重发。</p>
      <form @submit.prevent="loadHistory()"><fieldset :disabled="!tenantAdmin || historyBusy"><legend>历史筛选</legend>
        <label>历史节点 ID <input v-model="historyQuery.node" required maxlength="128" /></label>
        <label>历史状态 <select v-model="historyQuery.status"><option value="">全部</option><option v-for="status in ['pending','ok','error','unsupported']" :key="status">{{ status }}</option></select></label>
        <label>历史指令类型 <select v-model="historyQuery.type"><option value="">全部（含旧类型）</option><option v-for="kind in ['reload_config','reload_skills','switch_model','graceful_restart']" :key="kind">{{ kind }}</option></select></label>
        <button>查询指令历史</button>
      </fieldset></form>
      <p v-if="historyError" role="alert">{{ historyError }}</p>
      <table v-if="historyLoaded"><thead><tr><th>ID / 类型</th><th>状态</th><th>下发人 / 创建时间</th><th>回执</th></tr></thead><tbody>
        <tr v-for="row in historyRows" :key="row.id"><td>{{ row.id }} / {{ row.type }}</td><td>{{ row.status }}</td><td>{{ row.created_by }} / {{ row.created_at }}</td>
          <td><button :disabled="receiptBusy" @click="openReceipt(row.node_id, row.id)">查看此指令回执</button></td></tr>
        <tr v-if="!historyRows.length"><td colspan="4">当前筛选无记录；不代表未知请求未曾被受理。</td></tr>
      </tbody></table>
      <button v-if="historyCursor !== null" :disabled="historyBusy" @click="loadHistory(true)">加载更早指令</button>
    </section>
    <section aria-labelledby="reconcile-heading">
      <h3 id="reconcile-heading">下发审计对账 · 只读候选定位</h3>
      <p>复用 node.command 审计与回执接口，不自动推断请求唯一性或解除下发锁定。相同参数可能对应多次独立请求；查无记录也不能证明未受理。时间窗可调整，浏览器与服务端时钟偏差可能漏选。</p>
      <form @submit.prevent="loadAudit()"><fieldset :disabled="!tenantAdmin || auditBusy"><legend>当前租户 {{ operator.tenant }} 的审计条件</legend>
        <label>审计节点 ID <input v-model="auditQuery.node" required maxlength="128" /></label>
        <label>原操作者（可选）<input v-model="auditQuery.actor" maxlength="128" /></label>
        <label>审计开始（ISO 时区，可选）<input v-model="auditQuery.since" placeholder="2026-10-21T00:00:00Z" /></label>
        <label>审计结束（ISO 时区，可选）<input v-model="auditQuery.until" placeholder="2026-10-21T23:59:59Z" /></label>
        <button>查询下发审计</button>
      </fieldset></form>
      <p v-if="auditError" role="alert">{{ auditError }}</p>
      <table v-if="auditLoaded"><thead><tr><th>审计 ID / 时间</th><th>操作者 / 指令 ID</th><th>类型 / 候选提示</th><th>只读操作</th></tr></thead><tbody>
        <tr v-for="row in auditRows" :key="row.id"><td>{{ row.id }} / {{ row.ts }}</td><td>{{ row.actor }} / {{ row.detail.id }}</td><td>{{ row.detail.type }}
          <span v-if="unknownAttempt && operator.tenant === unknownAttempt.prepared.operator.tenant && auditQuery.node.trim() === unknownAttempt.prepared.node"> / {{ matchesAttempt(row, unknownAttempt.prepared.body) ? '类型与参数相同（非唯一证明）' : '与当次参数不同' }}</span></td>
          <td><button :disabled="receiptBusy" @click="openReceipt(row.object, row.detail.id!)">按候选 ID 查询回执</button></td></tr>
        <tr v-if="!auditRows.length"><td colspan="4">未找到候选；请检查原租户、操作者和时间窗，不可据此重发。</td></tr>
      </tbody></table>
      <button v-if="auditCursor !== null" :disabled="auditBusy" @click="loadAudit(true)">加载更早审计</button>
    </section>
    <section aria-labelledby="receipt-heading">
      <h3 id="receipt-heading">节点指令回执 · 只读</h3>
      <p>复用 S10 持久化回执，仅查询本租户指令。不会下发、重试或确认指令；pending 不代表已执行，ok 不证明重启已更换进程。</p>
      <form @submit.prevent="readReceipt"><fieldset :disabled="!tenantAdmin || receiptBusy"><legend>按节点与指令 ID 查询</legend>
        <label>节点 ID <input v-model="receiptQuery.node" required maxlength="128" list="receipt-nodes" /></label>
        <datalist id="receipt-nodes"><option v-for="node in nodes" :key="node.node_id" :value="node.node_id" /></datalist>
        <label>指令 ID <input v-model="receiptQuery.command" required maxlength="128" /></label><button>查询回执</button>
      </fieldset></form>
      <p v-if="receiptError" role="alert">{{ receiptError }}</p>
      <dl v-if="receipt"><dt>租户 / 节点 / 指令</dt><dd>{{ receipt.tenant_id }} / {{ receipt.node_id }} / {{ receipt.id }}</dd>
        <dt>类型 / 回执状态</dt><dd>{{ receipt.type }} / {{ receipt.status }}</dd>
        <dt>创建时间 / 回执时间</dt><dd>{{ receipt.created_at || '未知' }} / {{ receipt.acked_at || '尚未回执' }}</dd></dl>
      <p v-else>尚未查询或回执不可用；切换身份或查询条件后旧结果立即清空。参数和原始执行结果不在此展开。</p>
      <aside v-if="receipt" aria-label="回执诊断与下一步">
        <h4>回执诊断与下一步</h4><p>{{ nodeReceiptGuidance(receipt.type, receipt.status) }}</p>
        <p>只读恢复：保留当前节点与指令 ID，再次查询同一回执；结果未知时使用指令历史与审计候选定位。候选匹配不能证明唯一受理，也不会解锁再次下发。</p>
      </aside>
    </section>
    <section aria-labelledby="bindings-heading">
      <h3 id="bindings-heading">Agent 路由绑定 · {{ operator.tenant }}</h3>
      <form @submit.prevent="confirmBinding = true"><fieldset :disabled="busy || !tenantAdmin"><legend>配置本租户绑定</legend>
        <label>Agent ID <input v-model="agentId" @input="weights = Object.create(null)" @change="loadBinding" required maxlength="64" list="bound-agents" /></label>
        <datalist id="bound-agents"><option v-for="(_, name) in routing.bindings" :key="name" :value="name" /></datalist>
        <button type="button" @click="loadBinding">载入当前绑定</button>
        <div class="weights" v-for="group in groups" :key="group.name">
          <label><input type="checkbox" :checked="hasWeight(group.name)" @change="toggleBinding(group.name, ($event.target as HTMLInputElement).checked)" />{{ group.name }}</label>
          <label v-if="hasWeight(group.name)">权重 <input v-model.number="weights[group.name]" type="number" min="1" max="100" step="1" required :aria-label="`${group.name} 权重`" /></label>
        </div><button :disabled="!agentId.trim()">保存路由绑定</button>
        <div v-if="confirmBinding" role="group" aria-label="确认保存路由绑定">
          <p>确认覆盖 {{ operator.tenant }} / {{ agentId.trim() }} 的绑定？未勾选任何组将清空绑定。</p>
          <button type="button" :disabled="!agentId.trim()" @click="saveBindings">确认保存绑定</button><button type="button" @click="confirmBinding = false">取消保存</button>
        </div>
      </fieldset></form>
      <details><summary>查看当前租户健康路由快照（刷新更新）</summary><pre>{{ JSON.stringify(routing, null, 2) }}</pre></details>
    </section>
  </section>
</template>

<style scoped>
.nodes-page { color: #1f2937; }
header { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
h2 { margin-bottom: 8px; }
header p, .hint { color: #64748b; font-size: 13px; }
section section { margin-top: 28px; }
fieldset { border: 1px solid #d1d5db; border-radius: 6px; padding: 14px; margin-bottom: 14px; }
label { display: inline-flex; align-items: center; gap: 6px; margin: 4px 12px 4px 0; }
input, select, button { padding: 6px 8px; font: inherit; }
input[type="checkbox"] { width: auto; } input[type="number"] { width: 76px; }
button { margin: 3px; cursor: pointer; } button:disabled { cursor: not-allowed; opacity: .5; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td { text-align: left; border-bottom: 1px solid #e5e7eb; padding: 10px 8px; overflow-wrap: anywhere; }
th { background: #f8fafc; } small { color: #64748b; }
.error { color: #b91c1c; overflow-wrap: anywhere; }.healthy { color: #15803d; }.offline { color: #b45309; }
pre { overflow: auto; background: #f8fafc; padding: 12px; }
@media (max-width: 760px) { table { display: block; overflow-x: auto; } .operator label { display: flex; } }
</style>
