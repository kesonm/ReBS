import type { Adjust, HistoryResponse, StocksResponse } from './types'
import type { CandlePeriod, CandlesResponse, MarketsResponse, Snapshot } from './types'

async function request<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`/api${path}`, { signal })
  if (!response.ok) {
    let message = `请求失败（${response.status}）`
    try {
      const body = await response.json()
      if (typeof body.detail === 'string') message = body.detail
      else if (typeof body.message === 'string') message = body.message
    } catch { /* A proxy may return a non-JSON error. */ }
    throw new Error(message)
  }
  return response.json() as Promise<T>
}

export function searchStocks(query: string, signal?: AbortSignal) {
  return request<StocksResponse>(`/stocks?q=${encodeURIComponent(query)}`, signal)
}

export function getHistory(
  options: { symbol: string; adjust: Adjust; before?: number; limit?: number; refresh?: boolean },
  signal?: AbortSignal,
) {
  const params = new URLSearchParams({
    symbol: options.symbol,
    adjust: options.adjust,
    limit: String(options.limit ?? 400),
  })
  if (options.before !== undefined) params.set('before', String(options.before))
  if (options.refresh) params.set('refresh', 'true')
  return request<HistoryResponse>(`/history?${params}`, signal)
}

export const getMarkets = (signal?: AbortSignal) => request<MarketsResponse>('/okx/markets', signal)
export const getSnapshot = (instId: string, signal?: AbortSignal) => request<Snapshot>(`/okx/snapshot?instId=${encodeURIComponent(instId)}`, signal)
export function getCandles(instId: string, period: CandlePeriod, before?: number, signal?: AbortSignal) {
  const params = new URLSearchParams({ instId, period, limit: '300' })
  if (before !== undefined) params.set('before', String(before))
  return request<CandlesResponse>(`/okx/candles?${params}`, signal)
}
