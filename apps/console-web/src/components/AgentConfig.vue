<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { configObject, stringList, updateConfig } from "../agent-config";
const props = defineProps<{ modelValue: string }>();
const emit = defineEmits<{ "update:modelValue": [value: string] }>();
const error = ref("");
const parsed = computed(() => { try { return configObject(props.modelValue); } catch { return null; } });
const tools = ref("");
const redlines = ref("");
let emitted: string | null = null;
watch(() => props.modelValue, value => {
  const ownUpdate = value === emitted;
  emitted = null;
  if (ownUpdate) return;
  tools.value = Array.isArray(parsed.value?.tools) ? parsed.value.tools.join("\n") : "";
  const previous = parsed.value?.redlines as { deny_tools?: unknown } | undefined;
  redlines.value = Array.isArray(previous?.deny_tools) ? previous.deny_tools.join("\n") : "";
  error.value = "";
}, { immediate: true });
const skills = computed(() => {
  const value = parsed.value?.skills;
  return Array.isArray(value) && value.every(item => item && typeof item === "object" && typeof item.name === "string")
    ? value as Record<string, unknown>[] : null;
});
function update(key: "tools" | "skills" | "redlines", value: unknown) {
  try { emitted = updateConfig(props.modelValue, key, value); emit("update:modelValue", emitted); error.value = ""; }
  catch (reason) { error.value = String(reason); }
}
function skill(index: number, key: string, value: string) {
  update("skills", (skills.value ?? []).map((item, position) => position === index ? { ...item, [key]: value } : item));
}
</script>
<template>
  <p>结构化编辑仅修改对应配置项；未编辑字段与扩展字段原样保留。工具与红线每行或逗号分隔。未设置 tools 时 Runtime 默认使用 echo，显式清空则为 []。</p>
  <p v-if="!parsed" role="alert">JSON 无效，请先修正；结构化编辑已禁用，不会覆盖原文。</p>
  <p v-if="error" role="alert">{{ error }}</p>
  <fieldset :disabled="!parsed">
    <label>工具清单 <textarea v-model="tools" @input="update('tools', stringList(($event.target as HTMLTextAreaElement).value))" /></label>
    <label>红线禁止工具 <textarea v-model="redlines" @input="update('redlines', stringList(($event.target as HTMLTextAreaElement).value))" /></label>
    <p v-if="parsed?.skills !== undefined && !skills">已有 skills 格式不兼容，请用完整 JSON 编辑；不会自动转换或丢弃。</p>
    <fieldset :disabled="parsed?.skills !== undefined && !skills">
      <legend>Skill 引用（沿用 name / version 契约；发布时由服务端验证）</legend>
      <div v-for="(item, index) in skills ?? []" :key="index">
        <label>Skill 名称 <input :value="item.name" @input="skill(index, 'name', ($event.target as HTMLInputElement).value)" /></label>
        <label>Skill 版本 <input :value="item.version" @input="skill(index, 'version', ($event.target as HTMLInputElement).value)" /></label>
        <button type="button" @click="update('skills', skills?.filter((_, position) => position !== index))">移除此 Skill</button>
      </div>
      <button type="button" @click="update('skills', [...(skills ?? []), { name: '', version: '' }])">添加 Skill</button>
    </fieldset>
  </fieldset>
  <label style="display:block">完整 config JSON（高级编辑，保留扩展字段）<textarea :value="modelValue" rows="8" style="width:95%" required @input="emit('update:modelValue', ($event.target as HTMLTextAreaElement).value)" /></label>
</template>
