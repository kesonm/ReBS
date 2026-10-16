export type Scalar = string | number | boolean
export type DataStatus = 'available' | 'requires_history' | 'not_connected'
export interface ParameterDef {
  id: string; label: string; type: 'number' | 'integer' | 'boolean' | 'select' | 'string'
  default: Scalar; unit: string; description: string; min?: number; max?: number; step?: number
  options?: { value: Scalar; label: string }[]; group?: string
}
export interface FactorDefinition {
  id: string; name: string; category: string; description: string; formula: string; unit: string
  inputs: string[]; parameters: ParameterDef[]; dataSources: string[]; dataStatus: DataStatus
  computeStatus: string; notes: string | string[]; references?: string[]
}
export interface FactorInstance { alias: string; factorId: string; params: Record<string, Scalar> }
export interface CustomFactor { alias: string; name: string; expression: string; unit: string; description: string }
export interface StrategyDraft {
  id?: string; revision?: number; createdAt?: string; updatedAt?: string
  name: string; description: string; instrument: string; period: string
  parameters: Record<string, Scalar>; factors: FactorInstance[]; customFactors: CustomFactor[]
  rules: { entry: string; exit: string; filter: string }
}
export interface StrategyTemplate extends Omit<StrategyDraft, 'id' | 'revision'> { id: string }
export interface StrategyCatalog {
  version: string
  categories: { id: string; label: string; description: string }[]
  sources: { id: string; label: string; status: DataStatus; notes: string | string[] }[]
  factors: FactorDefinition[]; parameters: ParameterDef[]
  functions: { id: string; signature: string; description: string }[]
  templates: StrategyTemplate[]
}
export interface Issue { path: string; message: string }
export interface ValidationResult {
  valid: boolean; errors: Issue[]; warnings: Issue[]
  dependencies: { source: string; status: DataStatus }[]; normalized?: StrategyDraft
}
export interface PreviewResult {
  valid: boolean; results: Record<'entry' | 'exit' | 'filter', boolean | null>
  customValues: Record<string, number | boolean | null>; errors: Issue[]; warnings: Issue[]; mode: 'sample'
}
export const statusLabels: Record<DataStatus, string> = {
  available: '数据源已接入', requires_history: '需历史采集', not_connected: '数据源待接入',
}
export const LOCAL_DRAFT_KEY = 'rebs.strategy.draft.v1'
export const isSecureStrategyOrigin = () => window.location.protocol === 'https:' || ['127.0.0.1', 'localhost', '[::1]'].includes(window.location.hostname)
export const deepCopy = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T
export const parameterDefaults = (parameters: ParameterDef[]) => Object.fromEntries(parameters.map(p => [p.id, p.default]))
export const formatNotes = (value: string | string[] | undefined) => Array.isArray(value) ? value.join('；') : value ?? ''
export class StrategyRequestError extends Error {
  constructor(message: string, readonly status: number) { super(message) }
}
export async function strategyRequest<T>(path: string, options: { method?: string; body?: unknown; token?: string; signal?: AbortSignal } = {}): Promise<T> {
  const response = await fetch(`/api/strategy/${path}`, {
    method: options.method ?? (options.body === undefined ? 'GET' : 'POST'),
    cache: 'no-store', credentials: 'omit', signal: options.signal ? AbortSignal.any([options.signal, AbortSignal.timeout(25000)]) : AbortSignal.timeout(25000),
    headers: { ...(options.body === undefined ? {} : { 'Content-Type': 'application/json' }), ...(options.token ? { Authorization: `Bearer ${options.token}` } : {}) },
    ...(options.body === undefined ? {} : { body: JSON.stringify(options.body) }),
  })
  const result = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = result?.detail
    const errors = Array.isArray(detail?.errors) ? (detail.errors as Issue[]).map(issue => `${issue.path}: ${issue.message}`).join('；') : ''
    throw new StrategyRequestError(typeof detail === 'string' ? detail : errors || `请求失败（${response.status}），请检查配置后重试。`, response.status)
  }
  return result as T
}
export function emptyDraft(catalog: StrategyCatalog, instrument = 'BTC-USDT'): StrategyDraft {
  return { name: '我的策略草稿', description: '', instrument, period: '15m', parameters: parameterDefaults(catalog.parameters), factors: [], customFactors: [], rules: { entry: 'False', exit: 'False', filter: 'True' } }
}
export function fromTemplate(catalog: StrategyCatalog, template: StrategyTemplate): StrategyDraft {
  const { id: _templateId, ...content } = deepCopy(template)
  return { ...emptyDraft(catalog, template.instrument), ...content, parameters: { ...parameterDefaults(catalog.parameters), ...content.parameters } }
}
export function parseDraft(value: unknown, catalog: StrategyCatalog): StrategyDraft {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('文件必须包含策略 JSON 对象。')
  const wrapper = value as Record<string, unknown>
  const input = (wrapper.draft ?? value) as Partial<StrategyDraft>
  if (!input || typeof input !== 'object' || typeof input.name !== 'string' || typeof input.instrument !== 'string' || typeof input.period !== 'string'
    || !input.parameters || typeof input.parameters !== 'object' || Array.isArray(input.parameters)
    || !Array.isArray(input.factors) || !Array.isArray(input.customFactors) || !input.rules || typeof input.rules !== 'object'
    || !['entry', 'exit', 'filter'].every(k => typeof input.rules?.[k as keyof StrategyDraft['rules']] === 'string')) throw new Error('策略文件结构不完整，请导入工作台导出的 JSON 文件。')
  if (input.factors.some(f => !f || typeof f.alias !== 'string' || typeof f.factorId !== 'string' || !f.params || typeof f.params !== 'object' || Array.isArray(f.params))
    || input.customFactors.some(f => !f || !['alias','name','expression','unit','description'].every(k => typeof f[k as keyof CustomFactor] === 'string'))
    || Object.values(input.parameters).some(v => !['string','number','boolean'].includes(typeof v))) throw new Error('因子或参数格式不正确，当前草稿已保留。')
  return { ...emptyDraft(catalog, input.instrument), ...deepCopy(input as StrategyDraft), description: typeof input.description === 'string' ? input.description : '', parameters: { ...parameterDefaults(catalog.parameters), ...input.parameters } }
}
