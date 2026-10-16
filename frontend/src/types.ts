export type Adjust = 'qfq' | 'hfq' | 'none'

export interface Stock {
  symbol: string
  code: string
  name: string
  exchange: string
}

export interface Bar {
  timestamp: number
  open: number
  high: number
  low: number
  close: number
  volume: number
  amount: number
  turnoverRate?: number | null
}

export interface HistoryResponse {
  stock: Stock
  adjust: Adjust
  bars: Bar[]
  hasMore: boolean
  source: string
  fetchedAt: string
  cached: boolean
  stale: boolean
  total: number
  warning?: string
}

export interface StocksResponse {
  items: Stock[]
  limited: boolean
}

export interface ChartRange {
  from: number
  to: number
  count: number
}

export type CandlePeriod = '1s' | '1m' | '3m' | '5m' | '15m' | '30m' | '1H' | '2H' | '4H' | '1D' | '1W' | '1M'
export const PERIODS: CandlePeriod[] = ['1s', '1m', '3m', '5m', '15m', '30m', '1H', '2H', '4H', '1D', '1W', '1M']
export interface Instrument {
  instId: string
  base: string
  quote: string
  tickSize: string
  lotSize: string
  minSize: string
  pricePrecision: number
  volumePrecision: number
}
export interface Ticker {
  instId: string
  last: number
  open24h: number
  change24h: number | null
  high24h: number
  low24h: number
  volume24h: number
  quoteVolume24h: number
  bid: number
  ask: number
  ts: number
}
export type Market = Instrument & Ticker
export interface MarketsResponse { items: Market[]; source: string; fetchedAt: number }
export interface Candle extends Bar { confirmed: boolean }
export interface CandlesResponse { instId: string; period: CandlePeriod; bars: Candle[]; hasMore: boolean; source: string; fetchedAt: number }
export interface Book { asks: [number, number][]; bids: [number, number][]; ts: number; sequence?: number; depth: number }
export interface Trade { id: string; price: number; size: number; side: 'buy' | 'sell'; ts: number }
export interface Snapshot { instId: string; ticker: Ticker; book: Book; trades: Trade[]; tradeSource: string; source: string }
export type StreamState = 'connecting' | 'live' | 'reconnecting' | 'offline'
export type HistoryState = 'loading' | 'ready' | 'empty' | 'retrying' | 'error'
