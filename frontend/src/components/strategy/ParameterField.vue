<script setup lang="ts">
import type { ParameterDef, Scalar } from '../../strategy'
defineProps<{ definition: ParameterDef; modelValue: Scalar | undefined; prefix: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: Scalar] }>()
function numeric(event: Event) {
  const raw = (event.target as HTMLInputElement).value
  emit('update:modelValue', raw === '' ? '' : Number(raw))
}
</script>
<template>
  <div class="parameter-field">
    <label :for="`${prefix}-${definition.id}`"><span>{{ definition.label }}</span><small v-if="definition.unit">{{ definition.unit }}</small></label>
    <div v-if="definition.type === 'boolean'" class="boolean-field">
      <input :id="`${prefix}-${definition.id}`" type="checkbox" :checked="modelValue === true" @change="emit('update:modelValue', ($event.target as HTMLInputElement).checked)" />
      <span>{{ modelValue === true ? '已开启' : '已关闭' }}</span>
    </div>
    <select v-else-if="definition.type === 'select'" :id="`${prefix}-${definition.id}`" :value="String(modelValue ?? definition.default)" @change="emit('update:modelValue', definition.options?.find(o => String(o.value) === ($event.target as HTMLSelectElement).value)?.value ?? ($event.target as HTMLSelectElement).value)">
      <option v-for="option in definition.options" :key="String(option.value)" :value="String(option.value)">{{ option.label }}</option>
    </select>
    <input v-else-if="definition.type === 'number' || definition.type === 'integer'" :id="`${prefix}-${definition.id}`" type="number" :value="modelValue" :min="definition.min" :max="definition.max" :step="definition.step ?? (definition.type === 'integer' ? 1 : 'any')" @input="numeric" />
    <input v-else :id="`${prefix}-${definition.id}`" type="text" :value="modelValue" @input="emit('update:modelValue', ($event.target as HTMLInputElement).value)" />
    <p>{{ definition.description }}</p>
    <code>{{ definition.id }}</code>
    <small class="parameter-default">默认 {{ String(definition.default) }}<template v-if="definition.min !== undefined"> · 最小 {{ definition.min }}</template><template v-if="definition.max !== undefined"> · 最大 {{ definition.max }}</template></small>
  </div>
</template>
<style scoped>
.parameter-field{min-width:0;display:flex;flex-direction:column;gap:8px}.parameter-field label{display:flex;justify-content:space-between;gap:8px;font-size:12px;color:#dce6f3;line-height:1.5}.parameter-field label small{font-size:10px;color:#8495ad;font-weight:400}.parameter-field input:not([type=checkbox]),.parameter-field select{width:100%;height:37px;border:1px solid #2d3d52;border-radius:5px;background:#0a1422;color:#e0e9f4;padding:7px 10px;font-size:12px;outline:none}.parameter-field input:focus,.parameter-field select:focus{border-color:#a6ce61}.parameter-field p{font-size:11px;line-height:1.65;color:#8c9bb0;margin:0;overflow-wrap:anywhere}.parameter-field code{font-size:10px;color:#7c96b8;overflow-wrap:anywhere}.parameter-default{font-size:10px;color:#6d819e;line-height:1.6}.boolean-field{height:37px;display:flex;align-items:center;gap:8px;color:#aebed3;font-size:12px}.boolean-field input{accent-color:#c8f76b;width:17px;height:17px;margin:0}
</style>
