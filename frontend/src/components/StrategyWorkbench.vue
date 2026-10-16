<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowLeft, Check, Collection, DocumentCopy, Download, Filter, FolderOpened, Lock, Plus, Refresh, Search, Setting, Upload } from '@element-plus/icons-vue'
import ParameterField from './strategy/ParameterField.vue'
import type { Instrument } from '../types'
import { PERIODS } from '../types'
import {
  LOCAL_DRAFT_KEY, StrategyRequestError, deepCopy, emptyDraft, formatNotes, fromTemplate, isSecureStrategyOrigin,
  parameterDefaults, parseDraft, statusLabels, strategyRequest,
  type FactorDefinition, type FactorInstance, type ParameterDef, type PreviewResult, type Scalar,
  type StrategyCatalog, type StrategyDraft, type StrategyTemplate, type ValidationResult,
} from '../strategy'

const props = defineProps<{ instrument?: Instrument }>()
defineEmits<{ close: [] }>()
const catalog = ref<StrategyCatalog | null>(null)
const draft = ref<StrategyDraft | null>(null)
const loading = ref(true)
const loadError = ref('')
const activeTab = ref('catalog')
const category = ref('all')
const query = ref('')
const availability = ref('all')
const selectedId = ref('')
const inspectorParams = ref<Record<string, Scalar>>({})
const parameterQuery = ref('')
const functionQuery = ref('')
const activeParamGroup = ref('all')
const validation = ref<ValidationResult | null>(null)
const preview = ref<PreviewResult | null>(null)
const validationBusy = ref(false)
const previewBusy = ref(false)
const actionError = ref('')
const sampleText = ref('{\n\n}')
const localState = ref('等待加载')
const localSavedAt = ref('')
const templateOpen = ref(false)
const serverOpen = ref(false)
const serverPassword = ref('')
const serverUnlocked = ref(false)
const serverBusy = ref(false)
const serverError = ref('')
const serverDrafts = ref<StrategyDraft[]>([])
const importInput = ref<HTMLInputElement>()
const inspector = ref<HTMLElement>()
const secure = isSecureStrategyOrigin()
let token = ''
let saveAfterUnlock = false
let localTimer: ReturnType<typeof setTimeout> | undefined
let disposed = false
let editVersion = 0
let authEpoch = 0
let preserveInvalidLocal = false
const controller = new AbortController()
const tabs = [{ id: 'catalog', label: '因子字典', number: '01' }, { id: 'parameters', label: '策略参数', number: '02' }, { id: 'rules', label: '因子与公式', number: '03' }, { id: 'validate', label: '校验与试算', number: '04' }]
const ruleLabels = { entry: '买入条件', exit: '卖出条件', filter: '公共过滤条件' }
const ruleKeys = ['entry', 'exit', 'filter'] as const
const factorMap = computed(() => new Map(catalog.value?.factors.map(f => [f.id, f])))
const selected = computed(() => factorMap.value.get(selectedId.value))
const filteredFactors = computed(() => {
  const text = query.value.trim().toLowerCase()
  return catalog.value?.factors.filter(f => (category.value === 'all' || f.category === category.value)
    && (availability.value === 'all' || f.dataStatus === availability.value)
    && (!text || [f.id, f.name, f.description, f.formula, ...f.inputs, ...f.parameters.map(p => `${p.id} ${p.label}`)].join(' ').toLowerCase().includes(text))) ?? []
})
const parameterGroups = computed(() => [...new Set(catalog.value?.parameters.map(p => p.group ?? '其他参数') ?? [])])
const filteredParameterGroups = computed(() => parameterGroups.value.map(group => ({ group, parameters: (catalog.value?.parameters ?? []).filter(p => {
  const text = parameterQuery.value.trim().toLowerCase()
  return (p.group ?? '其他参数') === group && (activeParamGroup.value === 'all' || group === activeParamGroup.value)
    && (!text || `${p.id} ${p.label} ${p.description}`.toLowerCase().includes(text))
}) })).filter(g => g.parameters.length))
const selectedCount = computed(() => (draft.value?.factors.length ?? 0) + (draft.value?.customFactors.length ?? 0))
const functions = computed(() => catalog.value?.functions.filter(f => `${f.id} ${f.signature} ${f.description}`.toLowerCase().includes(functionQuery.value.toLowerCase())) ?? [])
const dependencySources = computed(() => {
  const used = new Set(draft.value?.factors.flatMap(f => factorMap.value.get(f.factorId)?.dataSources ?? []) ?? [])
  return catalog.value?.sources.filter(s => used.has(s.id)) ?? []
})
const warningSources = computed(() => dependencySources.value.filter(s => s.status !== 'available').length)
const visibleParameterCount = computed(() => filteredParameterGroups.value.reduce((sum, item) => sum + item.parameters.length, 0))
const hasStrategies = computed(() => Boolean(draft.value?.factors.length || draft.value?.customFactors.length))

function categoryName(id: string) { return catalog.value?.categories.find(c => c.id === id)?.label ?? id }
function sourceName(id: string) { return catalog.value?.sources.find(s => s.id === id)?.label ?? id }
function categoryCount(id: string) { return catalog.value?.factors.filter(f => f.category === id).length ?? 0 }
function factorName(instance: FactorInstance) { return factorMap.value.get(instance.factorId)?.name ?? instance.factorId }
function selectedParameters(instance: FactorInstance): ParameterDef[] { return factorMap.value.get(instance.factorId)?.parameters ?? [] }
function message(error: unknown) { return error instanceof Error ? error.message : '操作未完成，请稍后重试。' }
function stamp(value: unknown) {
  if (!value) return '尚未保存'
  const date = new Date(value as string | number)
  return Number.isNaN(date.valueOf()) ? String(value) : date.toLocaleString('zh-CN', { hour12: false })
}
function selectFactor(factor: FactorDefinition, reveal = false) {
  selectedId.value = factor.id
  inspectorParams.value = parameterDefaults(factor.parameters)
  if (reveal && window.innerWidth < 1250) requestAnimationFrame(() => inspector.value?.scrollIntoView({ behavior: 'smooth', block: 'start' }))
}
function persistLocal() {
  if (!draft.value) return
  if (preserveInvalidLocal) { localState.value = '旧草稿备份失败，请导出当前配置'; return }
  try {
    const savedAt = new Date().toISOString()
    localStorage.setItem(LOCAL_DRAFT_KEY, JSON.stringify({ schemaVersion: 1, catalogVersion: catalog.value?.version, savedAt, draft: draft.value }))
    localState.value = '已保存在此浏览器'
    localSavedAt.value = stamp(savedAt)
  } catch { localState.value = '浏览器保存不可用，请导出备份' }
}
watch(draft, () => {
  if (!draft.value) return
  editVersion++
  validation.value = null; preview.value = null; actionError.value = ''
  localState.value = '正在保存…'
  clearTimeout(localTimer)
  localTimer = setTimeout(persistLocal, 450)
}, { deep: true })
watch(sampleText, () => { preview.value = null })
async function loadCatalog() {
  loading.value = true; loadError.value = ''
  try {
    const data = await strategyRequest<StrategyCatalog>('catalog', { signal: controller.signal })
    if (disposed) return
    catalog.value = data
    let restored = false
    let saved: string | null = null
    try {
      saved = localStorage.getItem(LOCAL_DRAFT_KEY)
      if (saved) { draft.value = parseDraft(JSON.parse(saved), data); restored = true }
    } catch {
      if (saved) {
        try { localStorage.setItem(`${LOCAL_DRAFT_KEY}.recovery`, saved) }
        catch { preserveInvalidLocal = true }
      }
      ElMessage.warning(preserveInvalidLocal ? '无法恢复或备份旧草稿，已暂停本地自动保存；当前配置可导出。' : '本地草稿无法恢复，原内容已备份至浏览器 recovery 存储；已创建空白草稿。')
    }
    if (!restored) draft.value = emptyDraft(data, props.instrument?.instId)
    if (data.factors[0]) selectFactor(data.factors[0])
  } catch (error) { if (!disposed) loadError.value = message(error) }
  finally { loading.value = false }
}
async function confirmReplace(label: string) {
  if (!draft.value) return true
  try {
    await ElMessageBox.confirm(`将用${label}替换当前编辑中的草稿。需要保留当前配置时，请先导出 JSON 或保存到服务器。`, '替换当前草稿', { confirmButtonText: '替换草稿', cancelButtonText: '保留当前草稿', type: 'warning', closeOnClickModal: false })
    return true
  } catch { return false }
}
async function useTemplate(template: StrategyTemplate) {
  if (!catalog.value || !(await confirmReplace(`“${template.name}”模板`))) return
  draft.value = fromTemplate(catalog.value, template)
  templateOpen.value = false; activeTab.value = 'rules'
  ElMessage.success('模板已载入，可逐项修改参数和公式')
}
async function createEmpty() {
  if (!catalog.value || !(await confirmReplace('空白策略'))) return
  draft.value = emptyDraft(catalog.value, props.instrument?.instId)
  activeTab.value = 'parameters'
}
function uniqueAlias(base: string) {
  const normalized = base.toLowerCase().replace(/[^a-z0-9_]/g, '_').replace(/^[^a-z_]+/, '') || 'factor'
  const used = new Set([...(draft.value?.factors.map(f => f.alias) ?? []), ...(draft.value?.customFactors.map(f => f.alias) ?? []), ...(catalog.value?.parameters.map(p => p.id) ?? []), ...(catalog.value?.functions.map(f => f.id) ?? [])])
  let alias = normalized; let index = 2
  while (used.has(alias)) alias = `${normalized}_${index++}`
  return alias
}
function addFactor(factor = selected.value, parameters = inspectorParams.value) {
  if (!factor || !draft.value) return
  if (draft.value.factors.length >= 100) { ElMessage.warning('每份策略最多可配置 100 个因子实例，请先移除不需要的因子。'); return }
  draft.value.factors.push({ alias: uniqueAlias(factor.id), factorId: factor.id, params: deepCopy(parameters) })
  ElMessage.success(`已添加 ${factor.name}，可在“因子与公式”中编辑`)
}
function addCustom() {
  if ((draft.value?.customFactors.length ?? 0) >= 30) { ElMessage.warning('每份策略最多可配置 30 个自定义因子。'); return }
  draft.value?.customFactors.push({ alias: uniqueAlias('custom_factor'), name: '自定义因子', expression: '', unit: '', description: '' })
}
function exportDraft() {
  if (!draft.value) return
  const json = JSON.stringify({ schemaVersion: 1, catalogVersion: catalog.value?.version, exportedAt: new Date().toISOString(), draft: draft.value }, null, 2)
  const url = URL.createObjectURL(new Blob([json], { type: 'application/json' }))
  const link = document.createElement('a'); link.href = url
  link.download = `${draft.value.name.replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_').slice(0, 70) || 'strategy'}.json`
  link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000)
}
async function importDraft(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]; input.value = ''
  if (!file || !catalog.value) return
  try {
    if (file.size > 256 * 1024) throw new Error('策略文件不能超过 256 KB。')
    const imported = parseDraft(JSON.parse(await file.text()), catalog.value)
    if (!(await confirmReplace('导入文件'))) return
    delete imported.id; delete imported.revision; delete imported.createdAt; delete imported.updatedAt
    draft.value = imported; activeTab.value = 'validate'
    ElMessage.success('策略已导入为新草稿，请运行配置校验')
  } catch (error) { ElMessage.error(error instanceof SyntaxError ? 'JSON 语法不正确，当前草稿已保留。' : message(error)) }
}
async function validateDraft() {
  if (!draft.value || validationBusy.value) return
  validationBusy.value = true; actionError.value = ''; activeTab.value = 'validate'
  const version = editVersion
  try {
    const result = await strategyRequest<ValidationResult>('validate', { body: draft.value, signal: controller.signal })
    if (!disposed && version === editVersion) validation.value = result
  } catch (error) { if (!disposed) actionError.value = message(error) }
  finally { validationBusy.value = false }
}
async function runPreview() {
  if (!draft.value || previewBusy.value) return
  previewBusy.value = true; actionError.value = ''; preview.value = null
  const version = editVersion; const sample = sampleText.value
  try {
    const values: unknown = JSON.parse(sample)
    if (!values || typeof values !== 'object' || Array.isArray(values)) throw new Error('试算输入必须是以因子别名为键的 JSON 对象。')
    const result = await strategyRequest<PreviewResult>('preview', { body: { draft: draft.value, values }, signal: controller.signal })
    if (!disposed && version === editVersion && sample === sampleText.value) preview.value = result
  } catch (error) { if (!disposed) actionError.value = error instanceof SyntaxError ? '试算输入不是有效 JSON，请检查逗号、引号和括号。' : message(error) }
  finally { previewBusy.value = false }
}
function openServer(save = false) {
  saveAfterUnlock = save; serverOpen.value = true; serverError.value = ''
  if (serverUnlocked.value) { if (save) void saveServer(); else void refreshServer() }
}
function resetAuth() { authEpoch++; token = ''; serverPassword.value = ''; serverUnlocked.value = false; serverDrafts.value = []; serverBusy.value = false }
function handleServerError(error: unknown) {
  serverError.value = message(error)
  if (error instanceof StrategyRequestError && [401, 403].includes(error.status)) resetAuth()
}
async function unlockServer() {
  if (!secure || !serverPassword.value || serverBusy.value) return
  token = serverPassword.value; serverPassword.value = ''; serverBusy.value = true; serverError.value = ''
  const epoch = ++authEpoch
  try {
    const result = await strategyRequest<{ items: StrategyDraft[] }>('drafts', { token, signal: controller.signal })
    if (disposed || epoch !== authEpoch) return
    serverDrafts.value = result.items; serverUnlocked.value = true
  } catch (error) { if (!disposed && epoch === authEpoch) { resetAuth(); handleServerError(error) } }
  finally { if (epoch === authEpoch) serverBusy.value = false }
  if (!disposed && epoch === authEpoch && serverUnlocked.value && saveAfterUnlock) { saveAfterUnlock = false; await saveServer() }
}
async function refreshServer() {
  if (!serverUnlocked.value || serverBusy.value) return
  serverBusy.value = true; serverError.value = ''
  const epoch = authEpoch
  try {
    const result = await strategyRequest<{ items: StrategyDraft[] }>('drafts', { token, signal: controller.signal })
    if (!disposed && epoch === authEpoch) serverDrafts.value = result.items
  }
  catch (error) { if (!disposed && epoch === authEpoch) handleServerError(error) }
  finally { if (epoch === authEpoch) serverBusy.value = false }
}
async function saveServer() {
  if (!draft.value || !serverUnlocked.value || serverBusy.value) return
  serverBusy.value = true; serverError.value = ''
  const version = editVersion; const sentDraft = draft.value; const epoch = authEpoch
  try {
    const saved = await strategyRequest<StrategyDraft>(draft.value.id ? `drafts/${encodeURIComponent(draft.value.id)}` : 'drafts', { method: draft.value.id ? 'PUT' : 'POST', token, body: draft.value, signal: controller.signal })
    if (disposed || epoch !== authEpoch) return
    if (draft.value === sentDraft) {
      if (version === editVersion) draft.value = saved
      else { draft.value.id = saved.id; draft.value.revision = saved.revision; draft.value.updatedAt = saved.updatedAt }
    }
    serverDrafts.value = [saved, ...serverDrafts.value.filter(d => d.id !== saved.id)]
    ElMessage.success('策略配置已保存到服务器')
  } catch (error) { if (!disposed && epoch === authEpoch) handleServerError(error) }
  finally { if (epoch === authEpoch) serverBusy.value = false }
}
async function loadServer(item: StrategyDraft) {
  if (!catalog.value || !(await confirmReplace(`服务器策略“${item.name}”`))) return
  draft.value = parseDraft(item, catalog.value); serverOpen.value = false; activeTab.value = 'parameters'
}
async function deleteServer(item: StrategyDraft) {
  if (serverBusy.value || !item.id) return
  try {
    await ElMessageBox.confirm(`删除服务器上的“${item.name}”？本地正在编辑的草稿会保留。`, '删除服务器草稿', { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' })
  } catch { return }
  serverBusy.value = true; serverError.value = ''
  const epoch = authEpoch
  try {
    await strategyRequest(`drafts/${encodeURIComponent(item.id)}?revision=${item.revision}`, { method: 'DELETE', token, signal: controller.signal })
    if (disposed || epoch !== authEpoch) return
    serverDrafts.value = serverDrafts.value.filter(d => d.id !== item.id)
    if (draft.value?.id === item.id) { delete draft.value.id; delete draft.value.revision }
    ElMessage.success('服务器草稿已删除')
  } catch (error) { if (!disposed && epoch === authEpoch) handleServerError(error) }
  finally { if (epoch === authEpoch) serverBusy.value = false }
}
function flushLocal() { clearTimeout(localTimer); persistLocal() }
onMounted(() => { void loadCatalog(); window.addEventListener('pagehide', flushLocal) })
onBeforeUnmount(() => { disposed = true; controller.abort(); flushLocal(); resetAuth(); window.removeEventListener('pagehide', flushLocal) })
</script>

<template>
  <main class="strategy-workbench">
    <div class="strategy-hero">
      <div class="hero-heading"><button class="back-button" aria-label="返回行情" @click="$emit('close')"><el-icon><ArrowLeft /></el-icon></button><div><div class="strategy-eyebrow">STRATEGY LAB <span>配置工作台</span></div><h1>把交易想法，写成明确的规则。</h1><p>定义参数、组合因子、验证公式。每个输入和数据依赖都有据可查。</p></div></div>
      <div class="hero-metrics"><div><strong>{{ catalog?.factors.length ?? '—' }}</strong><span>因子定义</span></div><div><strong>{{ catalog?.parameters.length ?? '—' }}</strong><span>全局参数</span></div><div><strong>{{ selectedCount.toString().padStart(2, '0') }}</strong><span>已选因子</span></div></div>
    </div>
    <div v-if="loading" class="strategy-empty loading-state"><el-icon class="is-loading"><Refresh /></el-icon><h2>正在加载策略字典</h2><p>读取因子定义、参数约束和策略模板…</p></div>
    <div v-else-if="loadError" class="strategy-empty"><h2>策略工作台暂时无法加载</h2><p>{{ loadError }}</p><button class="primary-button" @click="loadCatalog">重新加载</button></div>
    <template v-else-if="catalog && draft">
      <div class="strategy-toolbar">
        <div class="draft-identity"><el-icon><DocumentCopy /></el-icon><div><strong>{{ draft.name || '未命名策略' }}</strong><span :title="localSavedAt"><i :class="{ saved: localState.startsWith('已保存') }" />{{ localState }}<template v-if="draft.revision"> · 服务器 v{{ draft.revision }}</template></span></div></div>
        <div class="toolbar-actions"><button @click="templateOpen = true"><el-icon><Collection /></el-icon>策略模板</button><button @click="importInput?.click()"><el-icon><Upload /></el-icon>导入</button><button @click="exportDraft"><el-icon><Download /></el-icon>导出</button><button class="server-library-button" @click="openServer()"><el-icon><FolderOpened /></el-icon>服务器草稿</button><button class="primary-button" @click="openServer(true)"><el-icon><Check /></el-icon>保存到服务器</button></div>
        <input ref="importInput" type="file" accept="application/json,.json" hidden aria-label="导入策略 JSON" @change="importDraft" />
      </div>
      <div class="capability-note"><span class="capability-dot" /><strong>当前支持配置、校验与公式试算</strong><span>因子目录是定义清单；数据已接入不代表因子已实现计算。回测与自动运行将在执行引擎接入后开放。</span></div>
      <div class="strategy-layout">
        <aside class="strategy-sidebar">
          <div class="sidebar-title"><el-icon><Collection /></el-icon><h2>因子分类</h2><span>{{ catalog.categories.length }}</span></div>
          <nav class="category-list" aria-label="因子分类"><button :class="{ active: category === 'all' }" @click="category = 'all'; activeTab = 'catalog'"><span>全部因子</span><em>{{ catalog.factors.length }}</em></button><button v-for="item in catalog.categories" :key="item.id" :class="{ active: category === item.id }" :title="item.description" @click="category = item.id; activeTab = 'catalog'"><span>{{ item.label }}</span><em>{{ categoryCount(item.id) }}</em></button></nav>
          <div class="sidebar-sources"><h3>数据连接状态</h3><div v-for="source in catalog.sources" :key="source.id" :title="formatNotes(source.notes)"><span class="source-dot" :class="source.status" /><span>{{ source.label }}</span><small>{{ statusLabels[source.status] }}</small></div><p>逐笔与盘口历史需要持续采集。状态仅描述数据来源，不代表策略可以运行。</p></div>
        </aside>
        <section class="strategy-editor">
          <nav class="editor-tabs" aria-label="策略编辑步骤"><button v-for="tab in tabs" :key="tab.id" :class="{ active: activeTab === tab.id }" @click="activeTab = tab.id"><span>{{ tab.number }}</span>{{ tab.label }}<small v-if="tab.id === 'rules' && selectedCount">{{ selectedCount }}</small></button></nav>
          <section v-if="activeTab === 'catalog'" class="catalog-view">
            <div class="section-heading"><div><h2>{{ category === 'all' ? '全部因子字典' : categoryName(category) }}</h2><p>查看公式、参数、单位与数据依赖，按需加入策略。</p></div><span class="count-badge">{{ filteredFactors.length }} 项</span></div>
            <div class="catalog-filters"><label class="search-field"><el-icon><Search /></el-icon><input v-model="query" aria-label="搜索因子名称、公式或参数" placeholder="搜索因子、公式、参数…" /><button v-if="query" aria-label="清空因子搜索" @click="query = ''">×</button></label><select v-model="availability" aria-label="筛选数据状态"><option value="all">全部数据状态</option><option value="available">数据源已接入</option><option value="requires_history">需历史采集</option><option value="not_connected">数据源待接入</option></select></div>
            <div class="catalog-column-labels"><span>因子 / 公式</span><span>参数 · 数据</span></div>
            <div class="factor-list"><article v-for="factor in filteredFactors" :key="factor.id" class="factor-row" :class="{ selected: selectedId === factor.id }"><button class="factor-main" @click="selectFactor(factor, true)"><div><strong>{{ factor.name }}</strong><span class="factor-id">{{ factor.id }}</span></div><p>{{ factor.description }}</p><code>{{ factor.formula }}</code></button><div class="factor-row-meta"><span class="data-tag" :class="factor.dataStatus">{{ statusLabels[factor.dataStatus] }}</span><small>{{ factor.parameters.length }} 个参数 · {{ factor.unit || '无量纲' }}</small><button class="add-factor-button" :disabled="draft.factors.length >= 100" :aria-label="`添加 ${factor.name}`" @click="addFactor(factor, parameterDefaults(factor.parameters))"><el-icon><Plus /></el-icon>添加</button></div></article><div v-if="!filteredFactors.length" class="strategy-empty compact-empty"><el-icon><Search /></el-icon><h3>没有匹配的因子</h3><p>尝试其他关键词，或切换数据状态。</p><button @click="query = ''; availability = 'all'; category = 'all'">清除筛选</button></div></div>
          </section>
          <section v-if="activeTab === 'parameters'" class="parameters-view">
            <div class="section-heading"><div><h2>策略基础与全局参数</h2><p>所有参数均可编辑。单位、默认值与约束在每个字段下方说明。</p></div></div>
            <div class="base-fields"><label>策略名称<input v-model="draft.name" maxlength="100" placeholder="给策略起一个名字" /></label><label>交易币对<input v-model="draft.instrument" placeholder="BTC-USDT" /></label><label>K 线周期<select v-model="draft.period"><option v-for="period in PERIODS" :key="period" :value="period">{{ period }}</option></select></label><label class="full-span">策略说明<textarea v-model="draft.description" rows="2" maxlength="2000" placeholder="记录交易思路、适用行情和观察目标" /></label></div>
            <div class="parameter-toolbar"><label class="search-field"><el-icon><Search /></el-icon><input v-model="parameterQuery" placeholder="搜索全部全局参数…" aria-label="搜索全局参数" /></label><select v-model="activeParamGroup" aria-label="筛选参数分组"><option value="all">全部参数分组</option><option v-for="group in parameterGroups" :key="group" :value="group">{{ group }}</option></select></div>
            <div class="parameter-count">显示 {{ visibleParameterCount }} / {{ catalog.parameters.length }} 个全局参数</div>
            <section v-for="group in filteredParameterGroups" :key="group.group" class="parameter-group"><h3><el-icon><Setting /></el-icon>{{ group.group }}<small>{{ group.parameters.length }} 项</small></h3><div class="parameter-grid"><ParameterField v-for="parameter in group.parameters" :key="parameter.id" v-model="draft.parameters[parameter.id]" :definition="parameter" prefix="global" /></div></section>
            <div v-if="!visibleParameterCount" class="strategy-empty compact-empty"><p>没有匹配的参数。</p><button @click="parameterQuery = ''; activeParamGroup = 'all'">显示全部参数</button></div>
          </section>
          <section v-if="activeTab === 'rules'" class="rules-view">
            <div class="section-heading"><div><h2>组合因子与交易公式</h2><p>使用因子别名和全局参数编写表达式；每个实例可设置独立周期。</p></div><button class="secondary-button" @click="activeTab = 'catalog'"><el-icon><Plus /></el-icon>添加因子</button></div>
            <div v-if="!draft.factors.length" class="empty-factor-state"><el-icon><Collection /></el-icon><strong>从因子字典开始组合</strong><p>可以添加多个同类因子，例如两个不同周期的均线。</p><button @click="activeTab = 'catalog'">浏览因子字典 →</button></div>
            <article v-for="(instance, index) in draft.factors" :key="index" class="instance-card"><div class="instance-heading"><span class="instance-index">{{ String(index + 1).padStart(2, '0') }}</span><div><h3>{{ factorName(instance) }}</h3><small>{{ instance.factorId }}</small></div><button aria-label="移除因子" class="remove-button" @click="draft.factors.splice(index, 1)">移除</button></div><label class="alias-field">公式引用别名<input v-model="instance.alias" placeholder="例如 fast_ma" spellcheck="false" /><small>英文字母开头，使用小写字母、数字和下划线，保持唯一。</small></label><div v-if="selectedParameters(instance).length" class="parameter-grid instance-parameters"><ParameterField v-for="parameter in selectedParameters(instance)" :key="parameter.id" v-model="instance.params[parameter.id]" :definition="parameter" :prefix="`factor-${index}`" /></div><p v-if="!factorMap.has(instance.factorId)" class="inline-warning">当前字典中不存在此因子，请修改或移除后校验。</p><button class="definition-link" @click="factorMap.get(instance.factorId) && selectFactor(factorMap.get(instance.factorId)!, true)">查看公式与数据依赖 ↗</button></article>
            <div class="section-heading custom-heading"><div><h2>自定义派生因子</h2><p>通过已选因子组合新公式，无需写 Python 文件。</p></div><button class="secondary-button" :disabled="draft.customFactors.length >= 30" @click="addCustom"><el-icon><Plus /></el-icon>新增公式</button></div>
            <article v-for="(custom, index) in draft.customFactors" :key="index" class="custom-card"><div class="custom-fields"><label>因子名称<input v-model="custom.name" /></label><label>引用别名<input v-model="custom.alias" spellcheck="false" /></label><label>输出单位<input v-model="custom.unit" placeholder="如 ratio、USDT" /></label></div><label class="expression-label">表达式<textarea v-model="custom.expression" spellcheck="false" rows="2" placeholder="例如 (fast_ma - slow_ma) / slow_ma" /></label><label class="custom-description">说明<input v-model="custom.description" placeholder="说明公式含义及分母为零时的处理" /></label><button class="remove-button" @click="draft.customFactors.splice(index, 1)">移除此公式</button></article>
            <div class="formula-section"><h2>买卖条件</h2><p>使用 <code>and</code>、<code>or</code>、<code>not</code> 组合条件，<code>True</code> / <code>False</code> 表示布尔值。公式应返回布尔结果。</p><label v-for="key in ruleKeys" :key="key" class="expression-label"><span><i :class="key" />{{ ruleLabels[key] }}<small>{{ key }}</small></span><textarea v-model="draft.rules[key]" :aria-label="ruleLabels[key]" spellcheck="false" rows="3" :placeholder="key === 'entry' ? '例如 cross_above(fast_ma, slow_ma)' : key === 'exit' ? '例如 cross_below(fast_ma, slow_ma)' : '例如 True'" /></label><div class="expression-hint">变量：<code v-for="factor in [...draft.factors, ...draft.customFactors]" :key="factor.alias">{{ factor.alias || '(未命名)' }}</code><span v-if="!hasStrategies">尚未添加因子</span><span>以及“策略参数”中的数值、整数或布尔参数 ID（不含文本与选项参数）</span></div></div>
            <details class="function-library" open><summary>可用公式函数 <span>{{ catalog.functions.length }}</span></summary><label class="search-field"><el-icon><Search /></el-icon><input v-model="functionQuery" aria-label="搜索公式函数" placeholder="搜索函数或说明" /></label><div class="function-grid"><article v-for="fn in functions" :key="fn.id"><code>{{ fn.signature }}</code><p>{{ fn.description }}</p></article></div></details>
            <button class="primary-button validate-cta" :disabled="validationBusy" @click="validateDraft"><el-icon><Check /></el-icon>{{ validationBusy ? '正在校验…' : '校验参数与公式' }}</button>
          </section>
          <section v-if="activeTab === 'validate'" class="validation-view">
            <div class="section-heading"><div><h2>配置校验</h2><p>检查参数范围、引用变量、公式语法与数据依赖。</p></div><button class="primary-button" :disabled="validationBusy" @click="validateDraft"><el-icon><Check /></el-icon>{{ validationBusy ? '校验中…' : '运行校验' }}</button></div>
            <div v-if="actionError" class="error-notice" role="alert">{{ actionError }}</div>
            <div v-if="!validation" class="validation-placeholder"><el-icon><Filter /></el-icon><p>编辑完成后运行校验。修改配置会清除旧结果。</p></div>
            <template v-else><div class="validation-result" :class="{ passed: validation.valid }"><el-icon><Check v-if="validation.valid" /><Filter v-else /></el-icon><div><strong>{{ validation.valid ? '配置结构与公式校验通过' : '配置需要修正' }}</strong><p>{{ validation.errors.length }} 个错误 · {{ validation.warnings.length }} 条提示。校验通过不代表策略已运行或具备回测数据。</p></div></div><ul v-if="validation.errors.length" class="issue-list errors"><li v-for="(issue, index) in validation.errors" :key="index"><code>{{ issue.path }}</code><span>{{ issue.message }}</span></li></ul><ul v-if="validation.warnings.length" class="issue-list warnings"><li v-for="(issue, index) in validation.warnings" :key="index"><code>{{ issue.path }}</code><span>{{ issue.message }}</span></li></ul></template>
            <div class="dependency-panel"><h3>本策略的数据依赖 <span>{{ dependencySources.length }}</span></h3><div v-for="source in dependencySources" :key="source.id"><strong>{{ source.label }}</strong><span class="data-tag" :class="source.status">{{ statusLabels[source.status] }}</span><p>{{ formatNotes(source.notes) }}</p></div><p v-if="!dependencySources.length">添加因子后，这里会列出所需的数据源。</p><p v-if="warningSources" class="inline-warning">有 {{ warningSources }} 个数据源需要补充，当前配置可保存用于后续实现。</p></div>
            <div class="preview-section"><div class="section-heading"><div><h2>公式样本试算</h2><p>输入你提供的样本，检查条件是否触发。</p></div><span class="sample-badge">手动样本 · 非回测</span></div><p class="sample-instructions">JSON 的键为因子别名，值为数字、布尔值或按时间升序排列的数组。交叉和滚动函数需要序列；全局参数由配置读取。这里不会调用交易接口，也不会自动生成行情。</p><div v-if="draft.factors.length" class="input-aliases"><span>可输入的因子：</span><code v-for="factor in draft.factors" :key="factor.alias">{{ factor.alias }}</code></div><label class="expression-label">样本 JSON<textarea v-model="sampleText" aria-label="公式试算样本 JSON" rows="9" spellcheck="false" placeholder='{"fast_ma": [10, 12], "slow_ma": [11, 11]}' /></label><button class="secondary-button" :disabled="previewBusy" @click="runPreview">{{ previewBusy ? '正在试算…' : '计算样本结果' }}</button><template v-if="preview"><div class="preview-result-grid"><div v-for="key in ruleKeys" :key="key"><span>{{ ruleLabels[key] }}</span><strong :class="{ truthy: preview.results[key] === true }">{{ preview.results[key] === null ? '无法计算' : preview.results[key] ? 'True · 满足' : 'False · 不满足' }}</strong></div></div><div v-if="Object.keys(preview.customValues).length" class="custom-values"><h3>派生因子结果</h3><div v-for="(value, key) in preview.customValues" :key="key"><code>{{ key }}</code><strong>{{ value === null ? '无法计算' : value }}</strong></div></div><ul v-if="preview.errors.length" class="issue-list errors"><li v-for="(issue, index) in preview.errors" :key="index"><code>{{ issue.path }}</code><span>{{ issue.message }}</span></li></ul><ul v-if="preview.warnings.length" class="issue-list warnings"><li v-for="(issue, index) in preview.warnings" :key="index"><code>{{ issue.path }}</code><span>{{ issue.message }}</span></li></ul></template></div>
          </section>
        </section>
        <aside ref="inspector" class="factor-inspector">
          <template v-if="selected"><div class="inspector-topline"><span>因子说明</span><span>{{ categoryName(selected.category) }}</span></div><div class="inspector-title"><div class="factor-symbol">ƒ</div><div><h2>{{ selected.name }}</h2><code>{{ selected.id }}</code></div></div><div class="inspector-badges"><span class="data-tag" :class="selected.dataStatus">{{ statusLabels[selected.dataStatus] }}</span><span class="definition-tag">公式定义</span></div><p class="inspector-description">{{ selected.description }}</p><section><h3>计算公式</h3><pre class="formula-display">{{ selected.formula }}</pre><div class="unit-row"><span>输出单位</span><strong>{{ selected.unit || '无量纲' }}</strong></div></section><section><h3>输入字段</h3><div class="input-chips"><code v-for="input in selected.inputs" :key="input">{{ input }}</code><span v-if="!selected.inputs.length">无额外输入</span></div></section><section><h3>数据依赖</h3><div v-for="source in selected.dataSources" :key="source" class="inspector-source"><span class="source-dot" :class="catalog.sources.find(s => s.id === source)?.status" /><span>{{ sourceName(source) }}</span></div></section><section v-if="selected.parameters.length"><h3>添加时的参数 <small>{{ selected.parameters.length }} 项</small></h3><div class="inspector-parameters"><ParameterField v-for="parameter in selected.parameters" :key="`${selected.id}-${parameter.id}`" v-model="inspectorParams[parameter.id]" :definition="parameter" :prefix="`inspect-${selected.id}`" /></div></section><section v-else class="no-parameters">此因子没有独立参数。</section><section v-if="formatNotes(selected.notes)" class="factor-notes"><h3>口径与边界</h3><p>{{ formatNotes(selected.notes) }}</p></section><section v-if="selected.references?.length" class="factor-references"><h3>参考来源</h3><a v-for="(reference, index) in selected.references" :key="reference" :href="/^https?:\/\//.test(reference) ? reference : undefined" target="_blank" rel="noopener noreferrer">{{ /^https?:\/\//.test(reference) ? `参考资料 ${index + 1} ↗` : reference }}</a></section><button class="primary-button inspector-add" :disabled="draft.factors.length >= 100" @click="addFactor()"><el-icon><Plus /></el-icon>添加到当前策略</button><p class="inspector-footnote">添加后可在“因子与公式”中修改别名与参数。同一因子可添加多个实例，每份策略最多 100 个因子实例和 30 个自定义因子。</p></template>
        </aside>
      </div>
      <footer class="strategy-footer"><span>字典版本 {{ catalog.version }} · {{ catalog.categories.length }} 个分类</span><span>当前环境：欧易模拟盘 · 配置不会自动下单</span><button @click="createEmpty">新建空白策略</button></footer>
      <el-dialog v-model="templateOpen" title="从策略模板开始" width="720px" class="strategy-dialog" :close-on-click-modal="false"><p class="dialog-description">模板包含因子实例、全局参数和规则，可作为可编辑的起点。模板本身不代表盈利效果。</p><div class="template-grid"><button v-for="template in catalog.templates" :key="template.id" @click="useTemplate(template)"><span>{{ template.instrument }} · {{ template.period }}</span><h3>{{ template.name }}</h3><p>{{ template.description }}</p><small>{{ template.factors.length }} 个因子 · 载入模板 →</small></button></div><div v-if="!catalog.templates.length" class="dialog-description">当前字典未提供模板，可从空白策略开始。</div></el-dialog>
      <el-dialog v-model="serverOpen" title="服务器策略草稿" width="650px" class="strategy-dialog" :close-on-click-modal="false"><template v-if="!secure"><div class="server-access-note"><el-icon><Lock /></el-icon><h3>通过安全连接管理服务器草稿</h3><p>请使用 HTTPS，或通过 SSH 转发访问本地工作台。浏览、编辑、校验与导出仍然可用。</p><code>ssh -N -L 127.0.0.1:18000:127.0.0.1:18080 ubuntu</code><a href="http://127.0.0.1:18000/" target="_blank" rel="noopener">打开本地工作台 ↗</a></div></template><form v-else-if="!serverUnlocked" class="server-login" @submit.prevent="unlockServer"><p class="dialog-description">使用配置模拟盘时设置的网页访问口令。口令仅保留在当前页面内存中。</p><label>网页访问口令<input v-model="serverPassword" type="password" autocomplete="current-password" placeholder="输入网页访问口令" aria-label="服务器草稿访问口令" /></label><button class="primary-button" :disabled="serverBusy || !serverPassword" type="submit">{{ serverBusy ? '正在验证…' : '解锁服务器草稿' }}</button></form><template v-else><div class="server-actions"><span>已解锁 · {{ serverDrafts.length }} 份草稿</span><button :disabled="serverBusy" @click="refreshServer">刷新</button><button @click="resetAuth">锁定</button><button class="primary-button" :disabled="serverBusy" @click="saveServer">{{ serverBusy ? '处理中…' : '保存当前策略' }}</button></div><p class="dialog-description">保存的是策略配置。相同草稿按版本更新；多人同时修改时会提示版本冲突。</p><div v-if="!serverDrafts.length" class="server-empty">还没有服务器草稿，可先保存当前策略。</div><article v-for="item in serverDrafts" :key="item.id" class="server-draft-row"><div><strong>{{ item.name }}</strong><span>{{ item.instrument }} · {{ item.period }} · v{{ item.revision }}</span><small>{{ stamp(item.updatedAt) }}</small></div><button :disabled="serverBusy" @click="loadServer(item)">载入</button><button class="remove-button" :disabled="serverBusy" @click="deleteServer(item)">删除</button></article></template><p v-if="serverError" class="error-notice" role="alert">{{ serverError }}</p></el-dialog>
    </template>
  </main>
</template>

<style scoped src="./strategy/workbench.css"></style>
