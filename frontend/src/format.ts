export function price(value: number | undefined | null, precision = 2) {
  return value === undefined || value === null || !Number.isFinite(value) ? '—' : value.toLocaleString('en-US', { minimumFractionDigits: precision, maximumFractionDigits: precision })
}
export function compact(value: number | undefined | null) {
  if (value === undefined || value === null || !Number.isFinite(value)) return '—'
  if (Math.abs(value) >= 1e9) return `${(value/1e9).toFixed(2)}B`
  if (Math.abs(value) >= 1e6) return `${(value/1e6).toFixed(2)}M`
  if (Math.abs(value) >= 1e3) return `${(value/1e3).toFixed(2)}K`
  return value.toLocaleString('en-US', { maximumFractionDigits: Math.abs(value) < 1 ? 12 : 4 })
}
export function percent(value: number | undefined | null) {
  return value === undefined || value === null || !Number.isFinite(value) ? '—' : `${value > 0 ? '+' : ''}${value.toFixed(2)}%`
}
export function time(value: number | undefined | null, seconds = true) {
  return value ? new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', ...(seconds ? { second: '2-digit' } : {}) }).format(value) : '—'
}
export function dateTime(value: number | undefined | null) {
  return value ? new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(value) : '—'
}
