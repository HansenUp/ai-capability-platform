<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";

interface Metadata { display_name: string; description: string }
interface DirectoryUser extends Metadata { id: number; tenant_id: string; issuer: string; subject: string }
interface UserPage { items: DirectoryUser[]; next_after_id: number | null; scope: string }
const saved = (() => { try { return JSON.parse(localStorage.getItem("console.operator") ?? "null"); } catch { return null; } })();
const op = reactive<{ user: string; role: string; tenant: string }>(saved ?? { user: "ops", role: "TENANT_ADMIN", tenant: "default" });
const identity = reactive({ ...op });
const tenant = reactive<Metadata>({ display_name: "", description: "" });
const form = reactive({ issuer: "", subject: "", display_name: "", description: "" });
const users = ref<DirectoryUser[]>([]);
const next = ref<number | null>(null);
const editing = ref<number | null>(null);
const removing = ref<number | null>(null);
const busy = ref(false);
const message = ref("");
const allowed = computed(() => ["TENANT_ADMIN", "PLATFORM_ADMIN"].includes(op.role));
let generation = 0;

function clearForm() {
  editing.value = null;
  Object.assign(form, { issuer: "", subject: "", display_name: "", description: "" });
}
function clearDirectory() {
  users.value = []; next.value = null; removing.value = null;
  Object.assign(tenant, { display_name: "", description: "" }); clearForm();
}

async function request<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const current = generation;
  const response = await fetch(`/console/directory${path}`, {
    method,
    headers: { "Content-Type": "application/json", "X-Console-User": op.user, "X-Console-Role": op.role, "X-Console-Tenant": op.tenant },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  if (current !== generation) throw new DOMException("目录读取上下文已改变", "AbortError");
  if (!response.ok) {
    if ([401, 403, 404].includes(response.status)) clearDirectory();
    throw new Error(`HTTP ${response.status}：目录请求失败，未展示服务端正文。`);
  }
  return (text ? JSON.parse(text) : null) as T;
}

async function perform(action: (current: number) => Promise<void>) {
  if (busy.value) return;
  const current = generation;
  busy.value = true;
  message.value = "";
  try { await action(current); }
  catch (error) { if (current === generation) message.value = String(error); }
  finally { if (current === generation) busy.value = false; }
}

async function readUsers(current: number, append = false) {
  const page = await request<UserPage>(`/users?limit=100${append && next.value !== null ? `&after_id=${next.value}` : ""}`);
  if (current !== generation) return;
  if (page.scope !== "display_metadata_only" || !Array.isArray(page.items) || page.items.some(user => user.tenant_id !== op.tenant)) {
    clearDirectory(); throw new Error("目录范围不匹配");
  }
  users.value = append ? [...users.value, ...page.items] : page.items;
  next.value = page.next_after_id;
}

async function load() {
  await perform(async (current) => {
    const response = await request<{ metadata: Metadata | null; tenant_id: string; scope: string }>("/tenant");
    if (current !== generation) return;
    if (response.tenant_id !== op.tenant || response.scope !== "display_metadata_only") {
      clearDirectory(); throw new Error("目录范围不匹配");
    }
    Object.assign(tenant, response.metadata ?? { display_name: "", description: "" });
    await readUsers(current);
  });
}

async function saveTenant() {
  await perform(async (current) => {
    await request("/tenant", "PUT", { ...tenant });
    if (current === generation) message.value = "租户展示元数据已保存；未创建或修改真实租户。";
  });
}

function edit(user: DirectoryUser) { editing.value = user.id; removing.value = null; Object.assign(form, user); }

async function saveUser() {
  await perform(async (current) => {
    const metadata = { display_name: form.display_name, description: form.description };
    await request(editing.value === null ? "/users" : `/users/${editing.value}`, editing.value === null ? "POST" : "PUT",
      editing.value === null ? { ...metadata, issuer: form.issuer, subject: form.subject } : metadata);
    if (current !== generation) return;
    clearForm();
    await readUsers(current);
    if (current === generation) message.value = "目录映射已保存；未授予账号或角色权限。";
  });
}

async function removeUser() {
  await perform(async (current) => {
    await request(`/users/${removing.value}`, "DELETE");
    if (current !== generation) return;
    removing.value = null;
    clearForm();
    await readUsers(current);
    if (current === generation) message.value = "仅删除展示映射；账号、Agent 分配、令牌与 Run 均未改变。";
  });
}

function applyIdentity() {
  if (busy.value) return;
  generation += 1;
  Object.assign(op, identity);
  users.value = [];
  next.value = null;
  removing.value = null;
  message.value = "";
  Object.assign(tenant, { display_name: "", description: "" });
  clearForm();
  try { localStorage.setItem("console.operator", JSON.stringify(op)); } catch {}
  void load();
}
onMounted(load);
onBeforeUnmount(() => { generation += 1; clearDirectory(); });
</script>

<template>
  <h2>租户与用户目录</h2>
  <p>仅 Console 映射与展示元数据。不是 IdP 账号管理或安全撤权入口；不修改角色、Agent 分配、令牌和在途 Run。</p>
  <fieldset :disabled="busy">
    <legend>当前操作者（开发态身份头，不等同生产鉴权）</legend>
    <form @submit.prevent="applyIdentity">
      <label>用户 <input v-model="identity.user" required maxlength="128" /></label>
      <label>角色 <select v-model="identity.role"><option v-for="role in ['USER', 'ADMIN', 'TENANT_ADMIN', 'PLATFORM_ADMIN']" :key="role">{{ role }}</option></select></label>
      <label>租户 <input v-model="identity.tenant" required maxlength="64" /></label>
      <button>应用开发身份</button>
    </form>
  </fieldset>
  <p role="status">{{ message }}</p>
  <p v-if="!allowed">需要当前租户的租户管理员权限。</p>
  <button :disabled="busy" @click="load">刷新</button>
  <fieldset :disabled="busy || !allowed">
    <legend>当前租户：{{ op.tenant }}（不可通过目录创建其他租户）</legend>
    <form @submit.prevent="saveTenant">
      <label>租户显示名称 <input v-model="tenant.display_name" required maxlength="128" /></label>
      <label>租户说明 <input v-model="tenant.description" maxlength="500" /></label>
      <button>保存租户元数据</button>
    </form>
  </fieldset>
  <fieldset :disabled="busy || !allowed">
    <legend>{{ editing === null ? '新增用户映射' : '编辑用户展示信息（身份不可变）' }}</legend>
    <form @submit.prevent="saveUser">
      <label>IdP issuer <input v-model="form.issuer" :disabled="editing !== null" required maxlength="255" /></label>
      <label>IdP subject <input v-model="form.subject" :disabled="editing !== null" required maxlength="128" /></label>
      <label>用户显示名称 <input v-model="form.display_name" required maxlength="128" /></label>
      <label>用户说明 <input v-model="form.description" maxlength="500" /></label>
      <button>{{ editing === null ? '新增映射' : '保存展示信息' }}</button>
      <button type="button" @click="clearForm">清空编辑</button>
    </form>
  </fieldset>
  <table border="1" cellpadding="6">
    <thead><tr><th>Issuer</th><th>Subject</th><th>显示名称</th><th>说明</th><th>操作</th></tr></thead>
    <tbody><tr v-for="user in users" :key="user.id">
      <td>{{ user.issuer }}</td><td>{{ user.subject }}</td><td>{{ user.display_name }}</td><td>{{ user.description }}</td>
      <td><button :disabled="busy || !allowed" @click="edit(user)">编辑</button>
        <button :disabled="busy || !allowed" @click="removing = user.id">删除映射</button></td>
    </tr></tbody>
  </table>
  <button v-if="next !== null" :disabled="busy" @click="perform(current => readUsers(current, true))">加载更多</button>
  <section v-if="removing !== null" role="alert">
    <p>确认删除目录映射 #{{ removing }}？这不会停用账号、撤销权限或终止 Run。</p>
    <button :disabled="busy || !allowed" @click="removeUser">确认仅删除映射</button>
    <button :disabled="busy" @click="removing = null">取消删除</button>
  </section>
</template>

<style scoped>
fieldset { margin: 16px 0; }
label { display: inline-block; margin: 6px; }
button { margin: 4px; }
table { width: 100%; border-collapse: collapse; overflow-wrap: anywhere; }
</style>
