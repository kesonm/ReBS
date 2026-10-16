import type { Adjust, HistoryResponse, StocksResponse } from './types'
import type { CandlePeriod, CandlesResponse, MarketsResponse, Snapshot } from './types'

export class MarketRequestError extends Error {
  constructor(message: string, public readonly retryable: boolean, public readonly status?: number) {
    super(message)
    this.name = 'MarketRequestError'
  }
}

async function request<T>(path: string, signal?: AbortSignal): Promise<T> {
  const controller = new AbortController()
  let timedOut = false
  const cancel = () => controller.abort()
  if (signal?.aborted) cancel()
  else signal?.addEventListener('abort', cancel, { once: true })
  // Bound the complete request, including a response body that never finishes.
  const timer = setTimeout(() => { timedOut = true; controller.abort() }, 30000)
  try {
    const response = await fetch(`/api${path}`, { signal: controller.signal })
    if (!response.ok) {
      let message = `请求失败（${response.status}）`
      try {
        const body = await response.json()
        if (typeof body.detail === 'string') message = body.detail
        else if (typeof body.message === 'string') message = body.message
      } catch { /* A proxy may return a non-JSON error. */ }
      throw new MarketRequestError(message, response.status >= 500 || [408, 429].includes(response.status), response.status)
    }
    return await response.json() as T
  } catch (error) {
    if (signal?.aborted) throw new DOMException('Request cancelled', 'AbortError')
    if (timedOut) throw new MarketRequestError('行情请求超时，正在等待恢复。', true)
    if (error instanceof TypeError) throw new MarketRequestError('行情网络连接暂不可用，请稍后重试。', true)
    throw error
  } finally {
    clearTimeout(timer)
    signal?.removeEventListener('abort', cancel)
  }
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
