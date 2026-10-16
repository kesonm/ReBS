import { computed, onBeforeUnmount, ref, shallowRef, watch, type Ref } from 'vue'
import { getMarkets, getSnapshot } from '../api'
import type { Book, Candle, CandlePeriod, Market, StreamState, Ticker, Trade } from '../types'

export function useOKX(instId: Ref<string>, period: Ref<CandlePeriod>) {
  const markets = shallowRef<Market[]>([])
  const marketError = ref('')
  const marketLoading = ref(true)
  const marketUpdated = ref(0)
  const ticker = shallowRef<Ticker | null>(null)
  const book = shallowRef<Book | null>(null)
  const trades = shallowRef<Trade[]>([])
  const liveBar = shallowRef<Candle | null>(null)
  const publicState = ref<StreamState>('connecting')
  const businessState = ref<StreamState>('connecting')
  const streamError = ref('')
  const snapshotError = ref('')
  const tradeSource = ref('等待逐笔成交')
  const lastMessage = ref(0)
  const revision = ref(0)
  const frozen = ref(false)
  const now = ref(Date.now())
  const stale = computed(() => !!lastMessage.value && now.value - lastMessage.value > 30000)
  const live = computed(() => publicState.value === 'live' && businessState.value === 'live' && !stale.value)
  let socket: WebSocket | undefined
  let reconnect: ReturnType<typeof setTimeout> | undefined
  let generation = 0
  let snapshotAbort: AbortController | undefined
  const marketAbort = new AbortController()
  let marketRequest = false
  let stopped = false
  let connectionAttempt = 0
  let queue: Array<Record<string, any>> = []
  let tradeBuffer = new Map<string, Trade>()
  let fullTrades = false
  let everLive = false

  async function refreshMarkets() {
    if (marketRequest) return
    marketRequest = true
    try {
      const result = await getMarkets(marketAbort.signal)
      markets.value = result.items
      marketUpdated.value = result.fetchedAt
      marketError.value = ''
    } catch (error) {
      if (!stopped) marketError.value = error instanceof Error ? error.message : '币对列表加载失败'
    } finally { marketLoading.value = false; marketRequest = false }
  }

  function flush() {
    if (!queue.length) return
    const messages = queue
    queue = []
    let changedTrades = false
    for (const message of messages) {
      if (message.type === 'ticker') {
        const incoming = message.data as Ticker
        if (!ticker.value || incoming.ts >= ticker.value.ts) ticker.value = incoming
      } else if (message.type === 'book') {
        const incoming = message.data as Book
        if (!book.value || incoming.ts >= book.value.ts) book.value = incoming
      } else if (message.type === 'candle') {
        const incoming = message.data as Candle
        if (!liveBar.value || incoming.timestamp >= liveBar.value.timestamp) liveBar.value = incoming
      } else if (message.type === 'trades') {
        if (!fullTrades) { tradeBuffer.clear(); fullTrades = true; tradeSource.value = 'trades-all · 逐笔成交' }
        for (const item of message.data as Trade[]) tradeBuffer.set(item.id, item)
        changedTrades = true
      }
    }
    if (changedTrades) {
      const sorted = [...tradeBuffer.values()].sort((a, b) => b.ts - a.ts || b.id.length - a.id.length || b.id.localeCompare(a.id)).slice(0, 300)
      tradeBuffer = new Map(sorted.map(item => [item.id, item]))
      if (!frozen.value) trades.value = sorted
    }
  }
  watch(frozen, value => { if (!value) trades.value = [...tradeBuffer.values()].sort((a,b) => b.ts - a.ts || b.id.localeCompare(a.id)).slice(0,300) })

  async function snapshot(current: number) {
    snapshotAbort?.abort()
    snapshotAbort = new AbortController()
    try {
      const data = await getSnapshot(instId.value, snapshotAbort.signal)
      if (current !== generation || stopped) return
      if (!ticker.value || data.ticker.ts > ticker.value.ts) ticker.value = data.ticker
      if (!book.value || data.book.ts > book.value.ts) book.value = data.book
      if (!fullTrades) {
        tradeBuffer = new Map(data.trades.map(item => [item.id, item]))
        trades.value = data.trades
        tradeSource.value = data.tradeSource
      }
      snapshotError.value = ''
    } catch (error) {
      if (current === generation && !stopped && !(error instanceof DOMException && error.name === 'AbortError')) snapshotError.value = error instanceof Error ? error.message : '行情快照加载失败'
    }
  }

  function connect(current: number) {
    if (stopped || current !== generation) return
    const url = new URL('/api/okx/stream', location.href)
    url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    url.searchParams.set('instId', instId.value)
    url.searchParams.set('period', period.value)
    const ws = new WebSocket(url)
    socket = ws
    ws.onopen = () => { if (current === generation) connectionAttempt = 0 }
    ws.onmessage = event => {
      if (current !== generation) return
      try {
        const message = JSON.parse(event.data)
        lastMessage.value = Date.now()
        if (message.type === 'status') {
          const state = message.state as StreamState
          const target = message.stream === 'public' ? publicState : businessState
          if (state === 'live' && message.stream === 'business') {
            if (everLive) { revision.value++; void snapshot(current) }
            everLive = true
          }
          target.value = state
          if (message.message) streamError.value = message.message
          if (publicState.value === 'live' && businessState.value === 'live') streamError.value = ''
        } else if (message.type === 'error') streamError.value = message.message
        else if (message.type !== 'heartbeat') queue.push(message)
      } catch { streamError.value = '实时消息解析失败，正在重新连接。'; ws.close() }
    }
    ws.onclose = () => {
      if (stopped || current !== generation) return
      publicState.value = 'reconnecting'; businessState.value = 'reconnecting'
      reconnect = setTimeout(() => connect(current), Math.min(15000, 1000 * 2 ** Math.min(connectionAttempt++, 4)))
    }
    ws.onerror = () => { if (current === generation) streamError.value = '实时连接暂不可用，正在重连。' }
  }

  function restart() {
    generation++
    clearTimeout(reconnect)
    socket?.close()
    queue = []; tradeBuffer.clear(); fullTrades = false; everLive = false
    ticker.value = null; book.value = null; trades.value = []; liveBar.value = null
    frozen.value = false; streamError.value = ''; snapshotError.value = ''; tradeSource.value = '等待逐笔成交'
    publicState.value = 'connecting'; businessState.value = 'connecting'; lastMessage.value = 0
    void snapshot(generation)
    connect(generation)
  }
  watch([instId, period], restart, { immediate: true })
  void refreshMarkets()
  const marketTimer = setInterval(() => { if (!document.hidden) void refreshMarkets() }, 15000)
  const frameTimer = setInterval(flush, 100)
  const clockTimer = setInterval(() => { now.value = Date.now() }, 1000)
  onBeforeUnmount(() => {
    stopped = true; generation++; socket?.close(); clearTimeout(reconnect)
    clearInterval(marketTimer); clearInterval(frameTimer); clearInterval(clockTimer)
    snapshotAbort?.abort(); marketAbort.abort()
  })
  return { markets, marketError, marketLoading, marketUpdated, ticker, book, trades, liveBar, publicState, businessState,
    streamError, snapshotError, tradeSource, lastMessage, revision, frozen, now, stale, live, refreshMarkets, restart }
}
