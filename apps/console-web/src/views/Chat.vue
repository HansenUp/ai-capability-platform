<script setup lang="ts">
import { computed, inject, onBeforeUnmount, reactive, ref, watch } from "vue";
import { demoContextKey } from "../demo-context";
import { answerValue, RuntimeRequestError, RuntimeRunClient, workbenchCards, type ApprovalCard, type WorkbenchEvent, type WorkbenchRun, type WorkbenchIdentity, type WriteAttempt } from "@ai-platform/sdk-web/run-client";

const props = defineProps<{ portalAgent?: string; portalTokenProvider?: () => Promise<string> }>();
const development = import.meta.env.DEV && !props.portalTokenProvider;
async function applyPortal() {
  if (!props.portalTokenProvider || busy.value) return;
  identity.mode = "token"; identity.token = ""; apply();
  message.value = "正在读取门户身份，旧身份和数据已清空；尚未允许新写。";
  const current = generation;
  try {
    const token = await props.portalTokenProvider();
    if (current !== generation) return;
    identity.mode = "token"; identity.token = token; apply();
    agent.value = props.portalAgent ?? "";
  } catch {
    if (current === generation) message.value = "门户身份读取失败，请重新登录后显式应用；未发送业务写。";
  }
}
const demo = inject(demoContextKey, null);
const identity = reactive({ mode: "token", token: "", subject: "human:alice", tenant: "default" });
const credentials = ref<Record<string, string>>({});
const applied = ref(false), busy = ref(false), uncertain = ref(false);
const activeIdentity = ref("");
const reading = ref(false);
const ledgerReady = ref(false), checking = ref(false), tokenMode = ref(false);
const verified = ref<WorkbenchIdentity | null>(null), activeAttempt = ref<WriteAttempt | null>(null), ackPreview = ref<WriteAttempt | null>(null);
const expectedAttempt = ref(""), expectedRun = ref("");
const expectedAction = ref<WriteAttempt["action"] | null>(null), expectedApproval = ref<{ call_id: string; params_hash?: string } | null>(null);
const closedFeedback = ref<WriteAttempt | null>(null);
const executionAnswer = ref("");
const feedbackScore = ref(1), feedbackRevision = ref<number | null>(null);
const expectedFeedback = ref<{ score: number; score_revision: number } | null>(null);
const executionCreate = ref(""), expectedCreateAgent = ref("");
const writeBlocked = computed(() => uncertain.value || !ledgerReady.value || activeAttempt.value !== null);
let ledgerController: AbortController | null = null;
const following = ref(false), followedEvents = ref<WorkbenchEvent[]>([]), followRun = ref(""), followCursor = ref(0);
let readController: AbortController | null = null;
const agent = ref(props.portalAgent ?? "demo"), text = ref(""), runId = ref(""), answer = ref("");
const history = ref<WorkbenchRun[]>([]), nextHistory = ref<string | null>(null);
async function loadHistory(next = false) {
  if (!props.portalTokenProvider || !verified.value) return;
  const subject = verified.value.subject, selectedAgent = agent.value, current = generation;
  const cursor = next ? nextHistory.value : null;
  await perform(async () => {
    history.value = []; nextHistory.value = null;
    const controller = new AbortController(); readController = controller; reading.value = true;
    try {
      const page = await client.listOwnRuns(selectedAgent, subject, cursor, controller.signal);
      if (current !== generation) return;
      history.value = page.items; nextHistory.value = page.next_cursor;
      message.value = "已读取本人当前 Agent 的 Run 记录；不是多轮会话，不包含他人审批收件箱。";
    } finally { if (readController === controller) { readController = null; reading.value = false; } }
  });
}
const run = ref<WorkbenchRun | null>(null), events = ref<WorkbenchEvent[]>([]), message = ref("");
const confirmation = ref<{ card: ApprovalCard; approve: boolean } | null>(null);
const cards = computed(() => run.value ? workbenchCards(run.value, events.value) : { approvals: [], input: null });
const responses = computed(() => events.value.filter(event => event.type === "model.response" && typeof event.payload.text === "string"));
const client = new RuntimeRunClient(async () => ({ ...credentials.value }));
let generation = 0;
function clear() { feedbackRevision.value = null; run.value = null; events.value = []; answer.value = ""; confirmation.value = null; }
function resetFollow() { followedEvents.value = []; followRun.value = ""; followCursor.value = 0; }
watch(runId, () => { clear(); resetFollow(); }, { flush: "sync" });
watch(runId, value => { if (demo && value) demo.run.value = value; }, { flush: "sync" });
function applyDemo() {
  if (!demo || busy.value) return;
  const selectedRun = demo.run.value;
  identity.mode = "token"; identity.token = demo.token(); apply();
  if (applied.value) { agent.value = demo.agent.value; runId.value = selectedRun; }
}
function apply() {
  if (following.value) stopReading();
  if (busy.value) return;
  history.value = []; nextHistory.value = null;
  generation++; closedFeedback.value = null; ledgerController?.abort(); ledgerController = null; checking.value = false;
  ledgerReady.value = false; verified.value = null; activeAttempt.value = null; ackPreview.value = null; expectedAttempt.value = ""; expectedRun.value = ""; expectedAction.value = null; expectedApproval.value = null;
  expectedFeedback.value = null; clear(); executionAnswer.value = ""; executionCreate.value = ""; expectedCreateAgent.value = ""; resetFollow(); credentials.value = {}; applied.value = false; runId.value = ""; text.value = "";
  if (identity.mode === "token" && identity.token.trim()) credentials.value = { Authorization: `Bearer ${identity.token.trim()}` };
  else if (identity.mode === "dev" && development && identity.subject.trim() && identity.tenant.trim()) {
    credentials.value = { "X-AI-Subject": identity.subject.trim(), "X-AI-Tenant": identity.tenant.trim() };
  } else { message.value = "请提供用户令牌或本机开发身份。"; return; }
  identity.token = ""; applied.value = true;
  tokenMode.value = identity.mode === "token";
  activeIdentity.value = identity.mode === "dev" ? `${identity.tenant} / ${identity.subject}` : "用户 Bearer 令牌（按服务端验签确定身份）";
  message.value = "身份已应用，旧 Run 与卡片已清空；令牌仅当前页面内存保存。未知写请求锁定不会因切换身份解除。";
  if (tokenMode.value) void checkLedger();
  else message.value = "开发身份仅允许只读定位；新写需要服务端验签用户身份与权威账本。";
}
async function checkLedger(): Promise<boolean> {
  if (!applied.value || !tokenMode.value) return false;
  const current = generation, previous = activeAttempt.value, scope = verified.value?.scope_id;
  ledgerController?.abort();
  const controller = new AbortController(); ledgerController = controller;
  ledgerReady.value = false; checking.value = true; ackPreview.value = null; closedFeedback.value = null;
  try {
    const result = await client.ledger(controller.signal);
    if (current !== generation || ledgerController !== controller) return false;
    if (!result.active && previous?.action === "feedback" && previous.scope_id === result.identity.scope_id) {
      const proof = await client.feedbackReceipt(previous.attempt_id, result.identity.scope_id, controller.signal);
      if (current !== generation || ledgerController !== controller) return false;
      if (proof.acknowledged && proof.status === "OBSERVED" && proof.run_id === previous.run_id && proof.request_seq === previous.request_seq
        && proof.fingerprint === previous.fingerprint && proof.score === previous.score && proof.score_revision === previous.score_revision
        && proof.payload_hash === previous.payload_hash && proof.revision > previous.revision) {
        verified.value = result.identity; closedFeedback.value = proof;
        message.value = "已读取本人已收口反馈的精确回执；仍保持锁定，须显式只读确认，不再执行ACK。";
        return false;
      }
    }
    if ((scope && result.identity.scope_id !== scope) || (previous && (!result.active || result.active.attempt_id !== previous.attempt_id))
      || (expectedAttempt.value && (result.active?.attempt_id !== expectedAttempt.value || (expectedRun.value ? result.active.run_id !== expectedRun.value : expectedAction.value !== "create")
        || result.active.action !== expectedAction.value || (expectedApproval.value && (result.active.call_id !== expectedApproval.value.call_id || result.active.params_hash !== expectedApproval.value.params_hash))))
      || (expectedFeedback.value && (result.active?.score !== expectedFeedback.value.score || result.active?.score_revision !== expectedFeedback.value.score_revision))
      || (expectedCreateAgent.value && result.active?.create_agent !== expectedCreateAgent.value)
      || (previous && result.active && (result.active.run_id !== previous.run_id || result.active.request_seq !== previous.request_seq
        || result.active.action !== previous.action || result.active.call_id !== previous.call_id || result.active.params_hash !== previous.params_hash || result.active.value_hmac !== previous.value_hmac
        || result.active.create_agent !== previous.create_agent || result.active.create_version !== previous.create_version || result.active.create_model !== previous.create_model
        || result.active.create_snapshot !== previous.create_snapshot || result.active.create_reason !== previous.create_reason
        || result.active.fingerprint !== previous.fingerprint || result.active.revision < previous.revision
        || result.active.score !== previous.score || result.active.score_revision !== previous.score_revision || result.active.payload_hash !== previous.payload_hash
        || (previous.action === "feedback" ? ["PREPARED", "PENDING_OBSERVATION", "OBSERVED"].indexOf(result.active.status) < ["PREPARED", "PENDING_OBSERVATION", "OBSERVED"].indexOf(previous.status)
          : previous.status !== "PREPARED" && (result.active.status !== previous.status || result.active.evidence_seq !== previous.evidence_seq || result.active.rejection !== previous.rejection))))) {
      throw new Error("账本与已知未决记录冲突，保持写锁，不以空列表解锁。");
    }
    verified.value = result.identity; activeAttempt.value = result.active; ledgerReady.value = true;
    message.value = result.active ? "已恢复服务端活动账本；终结回执不会自动解除写锁。" : "验签身份与账本读取完成；令牌仅在内存保存。";
    return result.active === null;
  } catch (failure) {
    if (current === generation && ledgerController === controller) message.value = String(failure);
    return false;
  } finally { if (ledgerController === controller) { checking.value = false; ledgerController = null; } }
}
async function preflight() {
  if (!await checkLedger() || uncertain.value) throw new Error("新写已锁定；请只读核对当前主体账本。");
}
async function ledgerAction(action: () => Promise<void>) {
  if (busy.value || checking.value || !applied.value || !tokenMode.value || uncertain.value) return;
  const current = generation; busy.value = true; ackPreview.value = null;
  try { await action(); }
  catch (failure) { if (current === generation) {
    if (failure instanceof RuntimeRequestError && [401, 403, 404].includes(failure.status)) clear();
    ledgerReady.value = false; message.value = `${String(failure)} 请只读核对账本；不得重发或自动解锁。`;
  } }
  finally { if (current === generation) busy.value = false; }
}
async function settle(acknowledge = false) {
  const row = acknowledge ? ackPreview.value : activeAttempt.value;
  if (!row || row.scope_id !== verified.value?.scope_id || !ledgerReady.value) return;
  let value: unknown;
  if (row.action === "create" && !acknowledge) {
    if (!executionCreate.value.trim()) { message.value = "请填写登记时的原始消息；尚未发送写请求。"; return; }
    value = executionCreate.value;
  }
  if (row.action === "answer" && !acknowledge) {
    try { value = JSON.parse(executionAnswer.value); }
    catch { message.value = "请填写登记时的原始 JSON 回答（字符串需引号）；尚未发送写请求。"; return; }
  }
  await ledgerAction(async () => {
    const current = generation;
    executionAnswer.value = ""; executionCreate.value = "";
    const receipt = await client.settleAttempt(row, acknowledge, value);
    if (current !== generation) return;
    activeAttempt.value = acknowledge ? null : receipt;
    if (acknowledge) { expectedAttempt.value = ""; expectedRun.value = ""; expectedAction.value = null; expectedApproval.value = null; expectedCreateAgent.value = ""; expectedFeedback.value = null; ledgerReady.value = false; await checkLedger(); }
    else {
      message.value = receipt.status === "PENDING_OBSERVATION" ? "反馈发送结果仍待只读对账；禁止重发，仍冻结全部新写。" : "已取得终结回执；仍冻结新写，须由原操作者显式确认收口。";
      if (receipt.action === "create" && receipt.status === "COMMITTED") { runId.value = receipt.run_id; await refresh(); }
    }
  });
}
async function confirmClosedFeedback() {
  if (busy.value || checking.value || !closedFeedback.value || closedFeedback.value.scope_id !== verified.value?.scope_id) return;
  closedFeedback.value = null; activeAttempt.value = null; expectedAttempt.value = ""; expectedRun.value = "";
  expectedAction.value = null; expectedApproval.value = null; expectedFeedback.value = null; expectedCreateAgent.value = "";
  ledgerReady.value = false;
  await checkLedger();
}
async function readFeedbackHead() {
  await perform(async () => {
    if (!run.value || !verified.value) return;
    const current = generation, id = run.value.run_id, scope = verified.value.scope_id;
    const controller = new AbortController(); readController = controller; reading.value = true;
    feedbackRevision.value = null;
    try {
      const revision = await client.feedbackHead(id, scope, controller.signal);
      if (current === generation && run.value?.run_id === id && verified.value?.scope_id === scope) feedbackRevision.value = revision;
    } finally { if (readController === controller) { readController = null; reading.value = false; } }
  });
}
async function registerFeedback() {
  if (!run.value || feedbackRevision.value === null || writeBlocked.value) return;
  const id = run.value.run_id, score = feedbackScore.value, revision = feedbackRevision.value;
  await ledgerAction(async () => {
    await preflight(); const current = generation;
    expectedAttempt.value = crypto.randomUUID(); expectedRun.value = id; expectedAction.value = "feedback";
    expectedApproval.value = null; expectedCreateAgent.value = ""; expectedFeedback.value = { score, score_revision: revision + 1 };
    const prepared = await client.prepareFeedback(id, score, revision, verified.value!.scope_id, expectedAttempt.value);
    if (current === generation) { activeAttempt.value = prepared; feedbackRevision.value = null; message.value = "反馈已登记，尚未发送；请核对分值与修订后显式执行一次。"; }
  });
}
async function reconcileFeedback() {
  const row = activeAttempt.value;
  if (!row || row.action !== "feedback" || row.status !== "PENDING_OBSERVATION" || !ledgerReady.value) return;
  await ledgerAction(async () => {
    const current = generation;
    const result = await client.feedbackAction(row, "reconcile");
    if (current === generation) { activeAttempt.value = result; message.value = result.status === "OBSERVED" ? "严格读回证据匹配；仍须原操作者显式确认收口。" : "尚无匹配来源证据，继续锁定；未重新发送评分。"; }
  });
}
async function refresh() {
  const current = generation;
  clear();
  const controller = new AbortController();
  readController = controller; reading.value = true;
  try {
    const result = await client.inspect(runId.value.trim(), controller.signal);
    if (current !== generation) return;
    run.value = result.run; events.value = result.events;
  } finally { if (readController === controller) { readController = null; reading.value = false; } }
}
function stopReading() {
  if (!readController) return;
  generation++; readController.abort(); readController = null; reading.value = false; following.value = false; busy.value = false; clear();
  message.value = "已停止本次只读刷新；未取消 Run，也未重发任何写请求。未知写锁定保持不变。";
}
async function follow() {
  await perform(async () => {
    const id = runId.value.trim(), current = generation;
    if (!id) throw new Error("Run ID 必填。");
    if (followRun.value !== id) resetFollow();
    clear(); followRun.value = id;
    const controller = new AbortController();
    readController = controller; reading.value = true; following.value = true;
    message.value = "正在只读跟随同 Run 已持久化事件；不是 Token 流。";
    try {
      for await (const event of client.follow(id, followCursor.value, controller.signal)) {
        if (current !== generation) return;
        followedEvents.value.push(event); followCursor.value = event.seq;
      }
      if (current === generation) message.value = "服务端已结束本次跟随；不推断 Run 完成，请手动读取状态与卡片。";
    } catch (failure) {
      if (current === generation && failure instanceof RuntimeRequestError && [401, 403, 404].includes(failure.status)) resetFollow();
      throw failure;
    } finally {
      if (readController === controller) { readController = null; reading.value = false; following.value = false; }
    }
  });
}
async function perform(action: () => Promise<void>) {
  if (busy.value || !applied.value) return;
  const current = generation;
  busy.value = true; message.value = ""; confirmation.value = null;
  try { await action(); }
  catch (failure) {
    if (current !== generation) return;
    if (failure instanceof RuntimeRequestError && [401, 403, 404].includes(failure.status)) clear();
    if (failure instanceof RuntimeRequestError && failure.outcomeUnknown) uncertain.value = true;
    message.value = String(failure);
  } finally { if (current === generation) busy.value = false; }
}
async function send() {
  if (writeBlocked.value) return;
  if (!agent.value.trim() || !text.value.trim()) return;
  const selectedAgent = agent.value.trim(), original = text.value;
  await ledgerAction(async () => {
    await preflight();
    const current = generation; clear();
    expectedAttempt.value = crypto.randomUUID(); expectedRun.value = ""; expectedAction.value = "create"; expectedApproval.value = null;
    expectedCreateAgent.value = selectedAgent;
    const prepared = await client.prepareCreate(selectedAgent, original, verified.value!.scope_id, expectedAttempt.value);
    if (current !== generation) return;
    activeAttempt.value = prepared; expectedRun.value = prepared.run_id; executionCreate.value = original; text.value = "";
    message.value = "创建已登记，仅预留 Run 标识，尚未创建或入队；请核对 Agent 快照并显式执行一次。";
  });
}
async function decide() {
  const pending = confirmation.value;
  if (!pending || !run.value || writeBlocked.value) return;
  const id = run.value.run_id;
  await ledgerAction(async () => { await preflight(); const current = generation;
    expectedAttempt.value = crypto.randomUUID(); expectedRun.value = id;
    expectedAction.value = pending.approve ? "approve" : "reject"; expectedApproval.value = { call_id: pending.card.call_id, params_hash: pending.card.params_hash };
    const prepared = await client.prepareApproval(id, pending.card, pending.approve, verified.value!.scope_id, expectedAttempt.value);
    if (current === generation) { activeAttempt.value = prepared; confirmation.value = null; message.value = "审批动作已登记，尚未批准或拒绝；请核对后显式执行一次。"; }
  });
}
async function provide() {
  if (!run.value || !cards.value.input || writeBlocked.value) return;
  const input = cards.value.input, id = run.value.run_id;
  if (input.kind === "credential") {
    await ledgerAction(async () => { await preflight(); const current = generation;
      expectedAttempt.value = crypto.randomUUID(); expectedRun.value = id;
      expectedAction.value = "credential"; expectedApproval.value = null;
      const prepared = await client.prepareCredential(id, verified.value!.scope_id, expectedAttempt.value);
      if (current === generation) { activeAttempt.value = prepared; message.value = "已完成写前登记，尚未刷新凭据；请核对后显式执行一次。"; }
    });
    return;
  }
  let value: unknown;
  try { value = answerValue(input, answer.value); }
  catch (failure) { message.value = String(failure); return; }
  await ledgerAction(async () => { await preflight(); const current = generation;
    expectedAttempt.value = crypto.randomUUID(); expectedRun.value = id;
    expectedAction.value = "answer"; expectedApproval.value = { call_id: input.call_id! };
    const prepared = await client.prepareAnswer(id, input, value, verified.value!.scope_id, expectedAttempt.value);
    if (current === generation) {
      activeAttempt.value = prepared; executionAnswer.value = JSON.stringify(value); answer.value = "";
      message.value = "回答已登记，尚未提交；原值仅当前页内存保存，重载后须重新输入原始 JSON 并显式执行一次。";
    }
  });
}
onBeforeUnmount(() => { generation++; readController?.abort(); ledgerController?.abort(); readController = null; ledgerController = null; credentials.value = {}; identity.token = ""; executionAnswer.value = ""; executionCreate.value = ""; verified.value = null; activeAttempt.value = null; clear(); resetFollow(); });
</script>

<template>
  <div class="workbench-head"><div class="page-kicker">AGENT WORKBENCH</div><h2>使用工作台 · 本机最小闭环</h2><p>发起任务、处理待办，并查看同一 Run 的执行进度。</p><div class="workflow-steps"><span>01 核对身份</span><span>02 登记与执行</span><span>03 审批 / 补充输入</span><span>04 本人确认回执</span></div></div>
  <button v-if="props.portalTokenProvider" :disabled="busy" @click="applyPortal">应用门户登录身份并核对账本</button>
  <button v-if="demo" :disabled="busy || !demo.role.value" @click="applyDemo">应用演示身份并定位同Run</button>
  <p>复用 SDK 与 Runtime Run API，直接使用用户身份，不使用 Console 服务账号。每次发送创建独立 Run；可手动跟随同 Run 已持久化事件，断连后手动续读，不自动重试写请求。不提供多轮会话、附件或 Token 流。</p>
  <p>创建、凭据刷新、单卡批准、拒绝和回答均先登记，再由本人显式执行及确认终结回执。创建登记只预留标识，不代表 Run 已创建；消息和回答原值不另存账本，重载后需重新输入。旧 API 的未登记请求不追溯纳入此协议。开发身份仅允许只读。</p>
  <fieldset v-if="!props.portalTokenProvider" :disabled="busy && !following"><legend>Runtime 身份（仅内存、不持久化）</legend>
    <label>鉴权方式 <select v-model="identity.mode"><option value="token">用户 Bearer 令牌</option><option v-if="development" value="dev">本机开发身份</option></select></label>
    <label v-if="identity.mode === 'token'">用户访问令牌 <input v-model="identity.token" type="password" autocomplete="off" /></label>
    <template v-else><label>主体 <input v-model="identity.subject" /></label><label>租户 <input v-model="identity.tenant" /></label></template>
    <button @click="apply">应用身份并清空旧数据</button>
  </fieldset>
  <p role="status">{{ message }}</p>
  <button v-if="reading" @click="stopReading">停止本次只读刷新</button>
  <p v-if="applied">当前已应用：{{ activeIdentity }}。上方身份字段仅为草稿，修改后须点击应用；不持久化令牌或消息。</p>
  <p v-if="identity.mode === 'dev'">开发态 Runtime 不强制身份或自批隔离，不能代替权限验收；隔离验收请使用强制 JWT 验签的测试身份。</p>
  <p v-if="uncertain" role="alert">写请求结果未知，已锁定所有写操作。只读刷新或到 Run 调试列表定位；不以查无记录或页面刷新作为重发依据。</p>
  <section v-if="applied && tokenMode" aria-label="权威写账本">
    <h3>当前主体写账本</h3><p v-if="verified">{{ verified.tenant_id }} / {{ verified.subject }} · {{ verified.scope_id }}</p>
    <p v-if="!ledgerReady" role="alert">账本尚未权威读取，禁止新写。</p>
    <button :disabled="busy || checking" @click="checkLedger">只读核对写账本</button>
    <section v-if="activeAttempt" aria-label="活动写回执"><p role="alert">存在活动账本，已锁定所有写操作；刷新、Run 终态或重新登录均不自动收口。</p>
      <p>attempt {{ activeAttempt.attempt_id }} · {{ activeAttempt.action }} · Run {{ activeAttempt.run_id }} · 请求序号 {{ activeAttempt.request_seq }}</p>
      <template v-if="activeAttempt.action === 'create'"><p>Agent {{ activeAttempt.create_agent }} / {{ activeAttempt.create_version }} · 模型 {{ activeAttempt.create_model }} · 快照 {{ activeAttempt.create_snapshot }} · 选版 {{ activeAttempt.create_reason }}。</p>
        <p v-if="activeAttempt.status !== 'COMMITTED'">此 Run 标识仅为预留，尚无创建提交回执；404 不解锁、不允许重新创建。</p>
        <label v-if="activeAttempt.status === 'PREPARED'">登记时的原始创建消息 <textarea v-model="executionCreate" :disabled="busy || checking || !ledgerReady" autocomplete="off" /></label>
      </template>
      <p v-if="activeAttempt.action === 'approve' || activeAttempt.action === 'reject'">调用 {{ activeAttempt.call_id }} · 参数指纹 {{ activeAttempt.params_hash }}。批准有效期固定为执行时起 900 秒，无附加审批意见；只处理当前单卡片。</p>
      <template v-if="activeAttempt.action === 'answer'"><p>调用 {{ activeAttempt.call_id }} · 回答绑定 {{ activeAttempt.value_hmac }}。账本不保存或恢复原始回答；重载后请重新输入同一 JSON 值，不得换值执行。</p>
        <label v-if="activeAttempt.status === 'PREPARED'">登记时的原始 JSON 回答 <textarea v-model="executionAnswer" :disabled="busy || checking || !ledgerReady" autocomplete="off" /></label>
      </template>
      <template v-if="activeAttempt.action === 'feedback'"><p>反馈分值 {{ activeAttempt.score }} · 评分修订 {{ activeAttempt.score_revision }} · 来源绑定 {{ activeAttempt.payload_hash }}。</p>
        <button v-if="closedFeedback" :disabled="busy || checking" @click="confirmClosedFeedback">确认已收口反馈（仅只读恢复）</button>
        <p>0 为中性，不是撤回。PENDING_OBSERVATION 不代表未发送，不允许重发；OBSERVED 仅证明严格读回，仍须本人确认。</p>
        <button v-if="activeAttempt.status === 'PENDING_OBSERVATION'" :disabled="busy || checking || !ledgerReady" @click="reconcileFeedback">只读对账反馈来源（不重发）</button>
      </template>
      <p>状态 {{ activeAttempt.status }} · 版本 {{ activeAttempt.revision }} · 指纹 {{ activeAttempt.fingerprint }} · 证据 {{ activeAttempt.evidence_seq ?? activeAttempt.rejection ?? '尚无终结回执' }}</p>
      <p v-if="activeAttempt.action !== 'feedback'">COMMITTED 只表示本次工作台写已提交，不代表工具执行成功或 Run 完成；查询及收口不会延长审批有效期。</p>
      <button v-if="activeAttempt.status === 'PREPARED'" :disabled="busy || checking || !ledgerReady || uncertain" @click="settle()">{{ activeAttempt.action === 'feedback' ? '确认发送已登记反馈一次' : activeAttempt.action === 'create' ? '确认执行已登记创建一次' : activeAttempt.action === 'credential' ? '确认执行已登记凭据刷新一次' : activeAttempt.action === 'approve' ? '确认执行已登记批准一次' : activeAttempt.action === 'answer' ? '确认执行已登记回答一次' : '确认执行已登记拒绝一次' }}</button>
      <button v-else-if="activeAttempt.status !== 'PENDING_OBSERVATION'" :disabled="busy || checking || !ledgerReady || uncertain" @click="ackPreview = { ...activeAttempt }">预览本人确认收口</button>
      <section v-if="ackPreview"><p>仅确认 {{ ackPreview.attempt_id }} / {{ ackPreview.fingerprint }} / 版本 {{ ackPreview.revision }}；不重新执行业务写。</p>
        <button :disabled="busy || checking || !ledgerReady" @click="settle(true)">确认收口此回执</button><button @click="ackPreview = null">取消收口</button>
      </section>
    </section>
  </section>
  <form @submit.prevent="send"><fieldset :disabled="busy || checking || !applied || writeBlocked"><legend>发送消息</legend>
    <label>Agent ID <input v-model="agent" :readonly="!!props.portalTokenProvider" required maxlength="64" /></label><label>消息 <textarea v-model="text" required /></label><button>登记新 Run（尚未创建）</button>
  </fieldset></form>
  <section v-if="props.portalTokenProvider && verified" aria-label="本人 Run 记录">
    <h3>当前 Agent · 本人 Run 记录</h3>
    <p>仅按 Runtime 验签主体查询；每条是独立 Run，不是多轮会话。只读列表不解除未决写锁。</p>
    <button :disabled="busy || checking" @click="loadHistory()">读取本人 Run 首页</button>
    <button :disabled="busy || checking || nextHistory === null" @click="loadHistory(true)">读取本人 Run 下一页</button>
    <ul><li v-for="item in history" :key="item.run_id">{{ item.run_id }} · {{ item.state }}
      <button :disabled="busy" @click="runId = item.run_id">定位此 Run（尚未读取）</button>
    </li></ul>
  </section>
  <fieldset :disabled="busy || !applied"><legend>定位与手动刷新</legend>
    <label>Run ID <input v-model="runId" /></label><button @click="perform(refresh)">读取 Run 与待办卡片</button>
    <button @click="follow">跟随／续读已持久化事件</button>
    <router-link v-if="!props.portalTokenProvider" to="/manage/runs">到 Run 只读调试列表</router-link>
  </fieldset>
  <section v-if="followRun" aria-label="已持久化事件跟随">
    <h3>只读跟随 {{ followRun }} · 已确认序号 {{ followCursor }}</h3>
    <p>仅当前身份与 Run 的内存游标；断连不自动重连，切换身份清空。done 只表示本次流结束，不替代 Run 状态或审批授权。</p>
    <ol><li v-for="event in followedEvents" :key="event.seq">{{ event.seq }} · {{ event.type }}<span v-if="event.type === 'model.response' && typeof event.payload.text === 'string'"> · {{ event.payload.text }}</span></li></ol>
  </section>
  <section v-if="run"><h3>{{ run.run_id }} · {{ run.state }} · 发起人 {{ run.subject }}</h3>
    <p v-for="event in responses" :key="event.seq">助手：{{ event.payload.text }}</p>
    <section v-for="card in cards.approvals" :key="card.call_id" class="card">
      <h4>审批卡片：{{ card.tool }}</h4><p>调用 {{ card.call_id }} · 参数指纹 {{ card.params_hash }}</p>
      <p>以下是 Runtime 日志返回的待批参数，不是重新计算的影响预演。强制鉴权时发起人不可自批；审批角色、租户、参数指纹与状态由 Runtime 复核。</p>
      <pre>{{ JSON.stringify(card.input, null, 2) }}</pre>
      <button :disabled="busy || checking || writeBlocked" @click="confirmation = { card, approve: true }">预览批准</button>
      <button :disabled="busy || checking || writeBlocked" @click="confirmation = { card, approve: false }">预览拒绝</button>
    </section>
    <section v-if="confirmation" class="card"><p>确认登记{{ confirmation.approve ? '批准' : '拒绝' }} {{ run.run_id }} / {{ confirmation.card.call_id }}？指纹 {{ confirmation.card.params_hash }}。此步仅写前登记，尚不批准或拒绝；批准有效期为显式执行时起 900 秒。</p>
      <button :disabled="busy || checking || writeBlocked" @click="decide">确认登记一次</button><button @click="confirmation = null">取消</button></section>
    <section v-if="cards.input" class="card"><h4>输入补充：{{ cards.input.kind }}</h4><p>{{ cards.input.prompt }}</p>
      <template v-if="cards.input.kind === 'answer'"><pre v-if="cards.input.schema">{{ JSON.stringify(cards.input.schema, null, 2) }}</pre>
        <p>{{ cards.input.schema ? '请提交 JSON 值（字符串也需引号）；Runtime 按 schema 最终校验。' : '请输入自由文本回答。' }}</p>
        <textarea v-model="answer" :disabled="busy || checking || writeBlocked" /><button :disabled="busy || checking || writeBlocked" @click="provide">登记回答一次</button></template>
      <template v-else-if="cards.input.kind === 'credential'"><p>重新应用发起人的有效用户令牌、按原 Run ID 定位后，才能刷新凭据；令牌不放请求体或日志。</p>
        <button :disabled="busy || checking || writeBlocked || !tokenMode" @click="provide">登记当前用户凭据刷新</button></template>
      <p v-else>未知写结果对账仅展示，不在本轮提供 done／skip／retry；不得以回答输入绕过对账。</p>
    </section>
    <section v-if="!props.portalTokenProvider && run.state === 'completed'" aria-label="可信反馈提交">
      <h4>可信反馈 · 本机协议</h4><p>仅原发起人可评分；不接评论或修正。登记、发送、来源对账与本人收口分开操作，不自动重投。</p>
      <fieldset :disabled="busy || checking || writeBlocked || !tokenMode || verified?.subject !== run.subject">
        <button @click="readFeedbackHead">读取反馈当前修订</button><p>当前修订 {{ feedbackRevision ?? '尚未读取' }}</p>
        <label>反馈分值 <select v-model.number="feedbackScore"><option :value="1">点赞 +1</option><option :value="0">中性 0（不撤回）</option><option :value="-1">点踩 -1</option></select></label>
        <button :disabled="feedbackRevision === null" @click="registerFeedback">登记反馈（尚未发送）</button>
      </fieldset>
    </section>
    <p v-if="!cards.approvals.length && !cards.input">当前读取范围内无待办卡片；不推断为完整授权或执行完成。</p>
  </section>
</template>
<style scoped>fieldset { margin:16px 0; padding:12px; } label { display:inline-flex; gap:8px; margin:6px; } button { margin:6px; } textarea { min-height:60px; } .card { border:1px solid #d97706; padding:12px; margin:12px 0; } pre { white-space:pre-wrap; overflow-wrap:anywhere; }</style>
