<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
const baseline = { version: "v1", columns: ["item", "secret"], filter_columns: ["item"], max_rows: 100 };
const form = reactive({ version: "v2", columns: ["item", "secret"], allowFilter: true, limit: 100 });
const error = ref(""), preview = ref("");
const changes = computed(() => [
  `版本：${baseline.version} → ${form.version || '未填写'}`,
  `投影列：${baseline.columns.join('、')} → ${form.columns.join('、') || '未选择'}`,
  `等值过滤列：item → ${form.allowFilter ? 'item' : '无'}`,
  `限行：100 → ${form.limit}`,
]);
watch(form, () => { error.value = ""; preview.value = ""; }, { deep: true, flush: "sync" });
function validate() {
  preview.value = ""; error.value = "";
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(form.version) || form.version === baseline.version) {
    error.value = "草稿需使用不同于v1的新版本标识，最多64个字母、数字、点、下划线或短横线。"; return;
  }
  if (!form.columns.length || form.columns.some(c => !baseline.columns.includes(c)) || (form.allowFilter && !form.columns.includes("item"))) {
    error.value = "至少选择一列；启用item等值过滤时必须保留item投影。"; return;
  }
  if (!Number.isInteger(form.limit) || form.limit < 1 || form.limit > 100) {
    error.value = "草稿限行必须为1–100的整数，不扩大当前隔离查询边界。"; return;
  }
  preview.value = JSON.stringify({ schema: "synthetic-view-proposal/v1", status: "local-draft-not-authorized",
    view_id: "orders", based_on_version: "v1", proposed_version: form.version,
    columns: [...form.columns], masked_columns: form.columns.includes("secret") ? ["secret"] : [],
    filter_columns: form.allowFilter ? ["item"] : [], max_rows: form.limit,
  }, null, 2);
}
function reset() { form.version = "v2"; form.columns = [...baseline.columns]; form.allowFilter = true; form.limit = 100; preview.value = ""; error.value = ""; }
</script>
<template>
  <section class="panel" aria-label="受限视图注册草稿">
    <h3>注册草稿与变更核对</h3>
    <p>以固定合成orders/v1示例起草。此处不是服务端注册清单或权限证明；仅保存在当前页面内存，重载即清空，不提交、不发布，也不改变下方v1查询。</p>
    <p>secret始终遮盖，不能用于过滤；租户血缘、源定义指纹、维护者与审核证明必须由后端可信来源确定，不能在浏览器自报已审核。</p>
    <form @submit.prevent="validate"><fieldset><legend>合成注册提案</legend>
      <label>草稿版本<input v-model="form.version" maxlength="64" /></label>
      <label><input v-model="form.columns" type="checkbox" value="item" />草稿item投影</label>
      <label><input v-model="form.columns" type="checkbox" value="secret" />草稿secret遮盖投影</label>
      <label><input v-model="form.allowFilter" type="checkbox" />草稿允许item等值过滤</label>
      <label>草稿限行<input v-model.number="form.limit" type="number" /></label>
      <button>校验并生成注册提案</button><button type="button" @click="reset">清空注册提案</button>
    </fieldset></form>
    <p v-if="error" role="alert">{{ error }}</p>
    <ul aria-label="注册变更对比"><li v-for="change in changes" :key="change">{{ change }}</li></ul>
    <section v-if="preview" aria-label="本机注册提案"><h4>待审核的本机提案</h4><pre>{{ preview }}</pre><p>校验通过只代表草稿格式可核对，不代表来源可信、审核通过或已发布；修改后需重新校验。</p></section>
    <p>注册管理待确认：租户内维护/审核权限、双人审核后的发布责任、版本冲突与撤销流程。签名下载待确认：身份和结果绑定、有效期、撤权、重放与清理；当前无下载链接。</p>
  </section>
</template>
