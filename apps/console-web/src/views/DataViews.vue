<script setup lang="ts">
import { reactive, ref, watch } from "vue";
import Chat from "./Chat.vue";
import DataPolicyRegistry from "../components/DataPolicyRegistry.vue";
import DataViewRegistry from "../components/DataViewRegistry.vue";
import DataViewDraft from "../components/DataViewDraft.vue";
import { previewRestrictedQuery, type QueryPreview } from "../restricted-query-preview";
import { proposeRestrictedQuery, QueryLanguageError } from "../restricted-query-language";
const language = ref("");
const selected = ref<string | null>(null);
const form = reactive({ version: "v1", columns: ["item"], filter: "", limit: 10 });
const preview = ref(""), error = ref("");
const languageFailure = ref<QueryLanguageError | null>(null);
const queryPlan = ref<QueryPreview | null>(null);
watch(form, () => { preview.value = ""; queryPlan.value = null; error.value = ""; languageFailure.value = null; }, { deep: true, flush: "sync" });
watch(language, () => { preview.value = ""; queryPlan.value = null; error.value = ""; languageFailure.value = null; });
function propose() {
  preview.value = ""; queryPlan.value = null; error.value = ""; languageFailure.value = null;
  try {
    const plan = proposeRestrictedQuery(language.value, form.version);
    queryPlan.value = plan; preview.value = JSON.stringify(plan.request, null, 2);
  } catch (failure) {
    languageFailure.value = failure instanceof QueryLanguageError ? failure : null;
    error.value = failure instanceof Error ? failure.message : "无法生成查询候选。";
  }
}
function build() {
  preview.value = ""; queryPlan.value = null; error.value = ""; languageFailure.value = null;
  try {
    const plan = previewRestrictedQuery(form);
    queryPlan.value = plan;
    preview.value = JSON.stringify(plan.request, null, 2);
  } catch (failure) {
    error.value = failure instanceof Error ? failure.message : "无法生成受限请求预览。";
  }
}
</script>
<template>
  <h2>受限视图 · 合成请求预览</h2>
  <p>先生成草稿，再显式登记和执行隔离查询；仅合成数据，不调用真实模型。合成清单不构成授权。</p>
  <section aria-label="合成视图权限说明">
    <h3>orders · 合成受限视图</h3>
    <p>item：允许投影与等值条件；secret：仅允许固定遮盖投影 [REDACTED]，禁止作为条件。租户列由服务端约束，不接受浏览器指定。</p>
    <p>实际执行必须验签真实调用者并取得精确视图/版本的read_data许可；管理员没有通配放行。读取前后复核身份、策略和清单，撤权或版本变化则丢弃旧结果。</p>
    <p>limit最多100，内核用额外一行判定截断；不承诺排序、分页或全量。大结果下载尚未接入，不生成虚假签名链接。</p>
  </section>
  <section class="panel" aria-label="F4统一体验指引">
    <h3>注册 → 授权 → 查询</h3>
    <nav aria-label="F4步骤导航"><a href="#f4-registration">1. 清单注册</a> · <a href="#f4-policy">2. 策略授权</a> · <a href="#f4-query">3. 受益人查询</a></nav>
    <ol>
      <li>在注册管理应用维护者身份，创建唯一清单版本并提交；切换两名注册审核者分别批准，再由原维护者发布。记下清单ID与清单版本。</li>
      <li>在策略管理使用独立策略角色，绑定清单ID、指定Alice或Bob并收窄列、过滤及限行。两名非本人、非受益审核者批准后，由原策略维护者发布。</li>
      <li>在查询表单填写清单版本（不是策略版本），范围不得超过策略；应用受益人身份，每次新建独立Run，查看工具结果表格与同Run轨迹。</li>
    </ol>
    <p>每次管理动作都需登记 → 只读恢复 → 显式执行一次 → 只读核对 → 本人ACK。三个区域独立应用身份，不自动传递令牌或执行操作。</p>
    <p>新库尚未发布清单时，v1仅供Alice固定测试；一旦发布新清单，不再回退v1。清单发布不授予read_data，策略active也必须匹配当前清单。</p>
    <details><summary>失败或结果未知时如何处理</summary><ul>
      <li>响应丢失或重载：使用原身份只读恢复对应账本；PREPARED才允许显式执行，终结回执只做本人ACK，不重新登记或自动重试。</li>
      <li>版本或策略头变化：先恢复并收口本次回执，再读取最新状态；旧草稿需撤回、编辑并重新双审。</li>
      <li>查询被拒：核对受益身份、清单版本、策略是否撤销及列/过滤/限行；Run完成不是查询成功，成功以同Run工具结果为准。</li>
      <li>撤权阻止后续查询，但不追回已交付结果；签名下载、真实数据及正式身份接入仍未验收。</li>
    </ul></details>
  </section>
  <DataViewDraft />
  <div id="f4-registration"><DataViewRegistry /></div>
  <div id="f4-policy"><DataPolicyRegistry /></div>
  <section class="panel" aria-label="自然语言受限查询">
    <h3>自然语言查询候选</h3>
    <p>本机规则解析，支持明确的订单明细问法；使用下方视图版本，不调用模型。候选不代表授权，确认后仍需本人登记并执行。</p>
    <p>示例：查询订单，商品等于“a1”，显示商品和遮盖信息，最多10行</p>
    <p>常见问法：查一下商品a1的订单，最多10条。不筛选时可写：查看订单，最多10条。</p>
    <p>商品按精确值匹配；省略投影时只显示商品。“最多”仅限制行数，不保证排序。不能回答金额、时间、状态、统计或跨租户问题。</p>
    <button @click="language = '查一下商品a1的订单，最多10条'">填入商品明细示例</button>
    <button @click="language = '查看订单，最多10条'">填入限行明细示例</button>
    <label>查询描述<textarea v-model="language" maxlength="512" /></label>
    <button @click="propose">生成自然语言查询候选</button>
  </section>
  <form id="f4-query" @submit.prevent="build">
    <fieldset><legend>仅合成参数，无SQL输入</legend>
      <label>视图版本 <input v-model="form.version" aria-label="视图版本" maxlength="64" /></label>
      <label><input v-model="form.columns" type="checkbox" value="item" />item投影</label>
      <label><input v-model="form.columns" type="checkbox" value="secret" />secret遮盖投影</label>
      <label>item等值条件（留空不筛选）<input v-model="form.filter" maxlength="128" /></label>
      <label>最多返回行数 <input v-model.number="form.limit" type="number" /></label>
      <button>生成受限请求预览</button>
    </fieldset>
  </form>
  <div v-if="error" role="alert"><p>{{ error }}</p><p v-if="languageFailure">{{ languageFailure.suggestion }}</p></div>
  <section v-if="preview" aria-label="受限请求草稿"><h3>待授权的请求草稿</h3><pre>{{ preview }}</pre><button @click="selected = '!tool data.query ' + preview">选择草稿进入隔离查询</button><p>已生成草稿，不代表查询成功。修改参数会清空；页面重载不保存条件。</p></section>
  <section v-if="queryPlan" class="panel" aria-label="受限查询解释">
    <h3>查询预览与解释</h3>
    <p>下面使用逻辑视图和逻辑租户列；执行服务会重新解析已登记的实际对象并核验身份与策略。</p>
    <ol><li v-for="line in queryPlan.explanation" :key="line">{{ line }}</li></ol>
    <h4>参数化逻辑模板（不可直接执行）</h4>
    <pre aria-label="参数化逻辑模板">{{ queryPlan.template }}</pre>
    <table aria-label="查询参数来源"><thead><tr><th>参数</th><th>来源</th><th>草稿值</th></tr></thead>
      <tbody><tr v-for="binding in queryPlan.bindings" :key="binding.name"><td>{{ binding.name }}</td><td>{{ binding.source }}</td><td>{{ binding.value === undefined ? "执行服务提供" : binding.value }}</td></tr></tbody>
    </table>
    <p>自然语言候选仅支持已说明的订单明细问法；未识别的条件会拒绝。请核对上方请求和参数后选择草稿；真实模型与数据库 EXPLAIN 尚未接入。</p>
  </section>
  <section aria-label="执行与下载待确认"><h3>执行与剩余边界</h3>
    <p>已接入Runtime→网关的既有身份委托和read_data核验链，不把Console开发身份头或告警管理员角色作为数据授权。每次查询新建独立Run，仅原发起人读取；由后端从持久事件构造委托上下文。终结回执需本人显式确认，不自动重试。</p>
    <p>签名下载仍需确认对象/主体/租户绑定、有效期、撤权、重放和审计；当前没有下载接口。</p>
  </section>
<section v-if="selected !== null" aria-label="隔离查询执行"><p>应用签名测试身份后填入草稿，再登记、执行一次、只读核对并本人确认收口。参数修改不会覆盖已登记请求。</p><Chat :isolated-query-text="selected" /></section>
</template>
