<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { dispose, init, type Chart, type DataLoaderGetBarsParams, type KLineData } from 'klinecharts'
import { getCandles, MarketRequestError } from '../api'
import type { Bar, Candle, CandlePeriod, CandlesResponse, ChartRange, HistoryState, Instrument } from '../types'

const props = defineProps<{
  instrument: Instrument
  period: CandlePeriod
  liveBar: Candle | null
  showMA: boolean
  showMACD: boolean
  refreshKey: number
  active: boolean
}>()

const emit = defineEmits<{
  loaded: [response: CandlesResponse]
  range: [range: ChartRange]
  error: [message: string]
  loading: [loading: boolean]
  more: [available: boolean]
  historyState: [state: HistoryState]
}>()

const host = ref<HTMLDivElement>()
const busy = ref(false)
const empty = ref(false)
const failure = ref('')
const paging = ref(false)
const retryWaiting = ref(false)
const RED = '#f45b6e'
const GREEN = '#21c7a0'
const NEUTRAL = '#8392a9'
const CANDLE_PANE = 'candle_pane'
const VOLUME_PANE = 'volume_pane'
const MACD_PANE = 'macd_pane'

let chart: Chart | null = null
let observer: ResizeObserver | undefined
let resizeFrame = 0
let generation = 0
let controller: AbortController | undefined
let activeRequest: Promise<void> | undefined
let bars: Candle[] = []
let hasMore = false
let paginationFailed = false
let liveUpdate: ((data: KLineData) => void) | undefined
let retryTimer: ReturnType<typeof setTimeout> | undefined
let retryAttempt = 0
let retryAt = 0

function clearRetry(resetAttempts = false) {
  clearTimeout(retryTimer)
  retryTimer = undefined
  retryWaiting.value = false
  if (resetAttempts) retryAttempt = 0
}

function resumeRetry() {
  clearTimeout(retryTimer)
  retryTimer = undefined
  if (!retryWaiting.value || !chart || document.hidden || !props.active || !navigator.onLine) return
  const current = generation
  retryTimer = setTimeout(() => {
    if (current !== generation || document.hidden || !props.active || !navigator.onLine) return
    reload(true)
  }, Math.max(250, retryAt - Date.now()))
}

function scheduleRetry() {
  retryAttempt += 1
  retryAt = Date.now() + Math.min(30000, 2000 * 2 ** Math.min(retryAttempt - 1, 4))
  retryWaiting.value = true
  emit('historyState', 'retrying')
  resumeRetry()
}

function applyLive(bar: Candle | null) {
  if (!bar || !liveUpdate || !bars.length) return
  const last = bars.at(-1)!
  if (bar.timestamp < last.timestamp) return
  if (bar.timestamp === last.timestamp) bars[bars.length - 1] = bar
  else { bars.push(bar); notifyRange() }
  liveUpdate(toChartBar(bar))
}

// OKX SPOT volume is base currency; chart turnover is quote currency.
function toChartBar(bar: Bar): KLineData {
  return {
    timestamp: bar.timestamp,
    open: bar.open,
    high: bar.high,
    low: bar.low,
    close: bar.close,
    volume: bar.volume,
    turnover: bar.amount,
  }
}

function normalizeBars(input: Candle[], before?: number): Candle[] {
  const byTimestamp = new Map<number, Candle>()
  for (const bar of input) {
    // Historical forward-adjusted prices can legitimately be zero or negative.
    if (![bar.timestamp, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.amount].every(Number.isFinite)) continue
    if (before !== undefined && bar.timestamp >= before) continue
    byTimestamp.set(bar.timestamp, bar)
  }
  return [...byTimestamp.values()].sort((a, b) => a.timestamp - b.timestamp)
}

function notifyRange() {
  emit('range', {
    from: bars[0]?.timestamp ?? 0,
    to: bars.at(-1)?.timestamp ?? 0,
    count: bars.length,
  })
}

function setLoading(value: boolean, isPage = false) {
  busy.value = value && !isPage
  paging.value = value && isPage
  emit('loading', value)
}

async function fetchBars(params: DataLoaderGetBarsParams) {
  if (params.type === 'backward') {
    params.callback([], { backward: false, forward: hasMore })
    return
  }

  const requestGeneration = generation
  const initial = params.type === 'init'
  const before = initial ? undefined : (params.timestamp ?? bars[0]?.timestamp)
  const requestController = new AbortController()
  controller = requestController
  setLoading(true, !initial)
  failure.value = ''
  emit('historyState', 'loading')

  try {
    const response = await getCandles(props.instrument.instId, props.period, before, requestController.signal)
    if (generation !== requestGeneration || requestController.signal.aborted || !chart) return

    const incoming = normalizeBars(response.bars, before)
    if (response.bars.length > 0 && incoming.length === 0) {
      throw new Error('行情数据格式异常，请刷新后重试。')
    }
    bars = initial ? incoming : normalizeBars([...incoming, ...bars])
    hasMore = response.hasMore && incoming.length > 0
    emit('more', hasMore)
    paginationFailed = false
    clearRetry(true)
    empty.value = bars.length === 0
    // Complete local state first: callback can immediately request another page.
    setLoading(false)
    emit('historyState', empty.value ? 'empty' : 'ready')
    if (initial) emit('loaded', { ...response, bars: incoming })
    notifyRange()
    params.callback(incoming.map(toChartBar), { forward: hasMore, backward: false })
    if (initial) applyLive(props.liveBar)
  } catch (error) {
    if (generation !== requestGeneration || requestController.signal.aborted || !chart) return
    const message = error instanceof Error ? error.message : '行情加载失败，请重试。'
    failure.value = message
    paginationFailed = !initial
    if (initial) {
      bars = []
      empty.value = true
      notifyRange()
    }
    setLoading(false)
    // Disable this boundary after an error to avoid an automatic retry loop.
    params.callback([], { forward: false, backward: false })
    emit('error', message)
    emit('historyState', 'error')
    if (initial && error instanceof MarketRequestError && error.retryable) scheduleRetry()
  } finally {
    if (controller === requestController) controller = undefined
  }
}

function loadChartData(params: DataLoaderGetBarsParams): Promise<void> {
  const request = fetchBars(params)
  activeRequest = request
  void request.finally(() => {
    if (activeRequest === request) activeRequest = undefined
  })
  return request
}

function reload(automatic = false) {
  if (!chart) return
  clearRetry(!automatic)
  generation += 1
  controller?.abort()
  controller = undefined
  activeRequest = undefined
  bars = []
  hasMore = false
  paginationFailed = false
  liveUpdate = undefined
  empty.value = false
  failure.value = ''
  notifyRange()
  // setSymbol starts a fresh v10 loading cycle and resets its internal loading flag.
  chart.setSymbol({
    ticker: props.instrument.instId,
    name: props.instrument.instId,
    pricePrecision: props.instrument.pricePrecision,
    volumePrecision: Math.min(props.instrument.volumePrecision, 8),
  })
}

function syncIndicators() {
  if (!chart) return
  const maExists = chart.getIndicators({ name: 'MA', paneId: CANDLE_PANE }).length > 0
  if (props.showMA && !maExists) {
    chart.createIndicator({
      name: 'MA',
      calcParams: [5, 10, 20],
      paneId: CANDLE_PANE,
      styles: {
        lines: [
          { color: '#e7b86b', size: 1.3 },
          { color: '#b195ee', size: 1.3 },
          { color: '#58a9ed', size: 1.3 },
        ],
      },
    }, true)
  } else if (!props.showMA && maExists) {
    chart.removeIndicator({ name: 'MA', paneId: CANDLE_PANE })
  }

  const macdExists = chart.getIndicators({ name: 'MACD', paneId: MACD_PANE }).length > 0
  if (props.showMACD && !macdExists) {
    chart.createIndicator({
      name: 'MACD',
      calcParams: [12, 26, 9],
      paneId: MACD_PANE,
      styles: { lines: [{ color: '#e7b86b' }, { color: '#b195ee' }] },
    })
    chart.setPaneOptions({ id: MACD_PANE, height: 112, minHeight: 70 })
  } else if (!props.showMACD && macdExists) {
    chart.removeIndicator({ name: 'MACD', paneId: MACD_PANE })
  }
}

function resetView() {
  if (!chart) return
  chart.setBarSpace(8)
  chart.scrollToRealTime()
}

function zoomIn() { chart?.zoomAtCoordinate(1.25) }
function zoomOut() { chart?.zoomAtCoordinate(0.8) }

async function loadMore(): Promise<void> {
  if (!chart) return
  if (activeRequest) return activeRequest
  if (paginationFailed || (failure.value && !bars.length)) {
    reload()
    return activeRequest
  }
  if (!hasMore || !bars.length) return

  // v10 exposes paging through its visible boundary. Trigger it and restore an
  // exact pixel anchor synchronously, before the browser paints either change.
  const timestamp = bars.at(-1)!.timestamp
  const original = chart.convertToPixel({ timestamp })
  chart.scrollToDataIndex(0)
  const shifted = chart.convertToPixel({ timestamp })
  if (!Array.isArray(original) && !Array.isArray(shifted)
      && original.x !== undefined && shifted.x !== undefined) {
    chart.scrollByDistance(original.x - shifted.x)
  }
  return activeRequest
}

watch(
  [() => props.instrument.instId, () => props.instrument.pricePrecision, () => props.refreshKey],
  () => reload(),
)
watch([() => props.showMA, () => props.showMACD], syncIndicators)
watch(() => props.liveBar, applyLive)
watch(() => props.active, resumeRetry)

onMounted(() => {
  if (!host.value) return
  chart = init(host.value, { locale: 'zh-CN', timezone: 'Asia/Shanghai' })
  if (!chart) {
    failure.value = '图表初始化失败，请刷新页面。'
    empty.value = true
    emit('error', failure.value)
    emit('historyState', 'error')
    return
  }
  chart.setStyles({
    grid: {
      horizontal: { color: '#192437', style: 'dashed', dashedValue: [3, 4] },
      vertical: { color: '#141f30', style: 'dashed', dashedValue: [3, 4] },
    },
    candle: {
      type: 'candle_solid',
      bar: {
        upColor: GREEN, downColor: RED, noChangeColor: NEUTRAL,
        upBorderColor: GREEN, downBorderColor: RED, noChangeBorderColor: NEUTRAL,
        upWickColor: GREEN, downWickColor: RED, noChangeWickColor: NEUTRAL,
      },
      priceMark: {
        high: { color: '#b9c8dc', textSize: 11 },
        low: { color: '#b9c8dc', textSize: 11 },
        last: { upColor: GREEN, downColor: RED, noChangeColor: NEUTRAL },
      },
      tooltip: {
        showType: 'standard',
        title: { show: false },
        legend: { color: '#9aacc4', size: 11 },
        offsetLeft: 12,
        offsetTop: 10,
      },
    },
    indicator: {
      ohlc: { upColor: GREEN, downColor: RED, noChangeColor: NEUTRAL },
      bars: [{ upColor: GREEN, downColor: RED, noChangeColor: NEUTRAL }],
      lines: [{ color: '#e7b86b', size: 1 }, { color: '#b195ee', size: 1 }],
      tooltip: {
        title: { color: '#91a3bc', size: 11 },
        legend: { color: '#91a3bc', size: 11 },
        offsetLeft: 12,
        offsetTop: 7,
      },
    },
    xAxis: {
      axisLine: { color: '#253146' },
      tickLine: { show: false },
      tickText: { color: '#71849f', size: 11, family: 'Inter, Microsoft YaHei, sans-serif' },
    },
    yAxis: {
      axisLine: { show: false },
      tickLine: { show: false },
      tickText: { color: '#71849f', size: 11, family: 'Inter, Microsoft YaHei, sans-serif' },
    },
    separator: { color: '#253146', size: 1, fill: true, activeBackgroundColor: '#33445c' },
    crosshair: {
      horizontal: {
        line: { color: '#607490', style: 'dashed', dashedValue: [4, 3] },
        text: { color: '#e2eafa', backgroundColor: '#34455f', borderColor: '#34455f' },
      },
      vertical: {
        line: { color: '#607490', style: 'dashed', dashedValue: [4, 3] },
        text: { color: '#e2eafa', backgroundColor: '#34455f', borderColor: '#34455f' },
      },
    },
  })
  chart.setOffsetRightDistance(52)
  chart.setBarSpace(8)
  chart.setZoomEnabled(true)
  chart.setScrollEnabled(true)
  chart.createIndicator({
    name: 'VOL', calcParams: [5, 10],
    paneId: VOLUME_PANE,
  })
  chart.setPaneOptions({ id: VOLUME_PANE, height: 102, minHeight: 64 })
  syncIndicators()
  chart.setDataLoader({
    getBars: loadChartData,
    subscribeBar: ({ callback }) => { liveUpdate = callback; applyLive(props.liveBar) },
    unsubscribeBar: () => { liveUpdate = undefined },
  })
  const periods = { '1s': ['second', 1], '1m': ['minute', 1], '3m': ['minute', 3],
    '5m': ['minute', 5], '15m': ['minute', 15], '30m': ['minute', 30], '1H': ['hour', 1],
    '2H': ['hour', 2], '4H': ['hour', 4], '1D': ['day', 1], '1W': ['week', 1], '1M': ['month', 1] } as const
  const [type, span] = periods[props.period]
  chart.setPeriod({ type, span })
  reload()

  observer = new ResizeObserver(() => {
    cancelAnimationFrame(resizeFrame)
    resizeFrame = requestAnimationFrame(() => chart?.resize())
  })
  observer.observe(host.value)
  document.addEventListener('visibilitychange', resumeRetry)
  window.addEventListener('online', resumeRetry)
  window.addEventListener('offline', resumeRetry)
})

onBeforeUnmount(() => {
  generation += 1
  clearRetry(true)
  controller?.abort()
  document.removeEventListener('visibilitychange', resumeRetry)
  window.removeEventListener('online', resumeRetry)
  window.removeEventListener('offline', resumeRetry)
  observer?.disconnect()
  cancelAnimationFrame(resizeFrame)
  if (host.value) dispose(host.value)
  chart = null
})

defineExpose({ resetView, zoomIn, zoomOut, loadMore, getBars: () => [...bars] })
</script>

<template>
  <div class="stock-chart" :aria-busy="busy || paging">
    <div ref="host" class="stock-chart__canvas" role="img" :aria-label="`${instrument.instId} ${period} K线、成交量${showMACD ? '及MACD' : ''}，可滚轮缩放和拖动`" />
    <div v-if="busy || empty" class="stock-chart__state" role="status">
      <template v-if="busy">
        <span class="stock-chart__spinner" />
        <strong>正在加载 {{ instrument.instId }}</strong>
        <span>整理历史 K 线与技术指标</span>
      </template>
      <template v-else>
        <span class="stock-chart__empty-icon">⌁</span>
        <strong>{{ failure ? '行情暂时无法加载' : '暂无 K 线数据' }}</strong>
        <span>{{ failure || '请切换币对或周期，或稍后重新获取。' }}</span>
        <span v-if="retryWaiting">网络恢复后将自动重试，也可立即重新加载。</span>
        <button type="button" @click="reload()">重新加载</button>
      </template>
    </div>
    <div v-if="paging" class="stock-chart__paging" role="status">
      <span class="stock-chart__spinner" /> 加载更早的 K 线…
    </div>
    <div v-else-if="failure && !empty" class="stock-chart__page-error" role="status">
      历史加载失败 · <button type="button" @click="reload()">重新加载</button>
    </div>
  </div>
</template>

<style scoped>
.stock-chart { position: relative; isolation: isolate; width: 100%; height: 100%; min-height: 360px; background: #0c1422; overflow: hidden; }
.stock-chart__canvas { position: relative; z-index: 0; width: 100%; height: 100%; }
.stock-chart__state { position: absolute; z-index: 2; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 13px; color: #7d90ab; font-size: 12px; padding: 32px; text-align: center; background: #0c1422; }
.stock-chart__state strong { color: #c7d3e6; font-size: 15px; font-weight: 500; }
.stock-chart__state > span:not(.stock-chart__spinner):not(.stock-chart__empty-icon) { max-width: 460px; line-height: 1.7; }
.stock-chart__state button { padding: 8px 17px; margin-top: 4px; border: 1px solid #365474; border-radius: 6px; background: #152b43; color: #8fc6ff; cursor: pointer; font: inherit; }
.stock-chart__empty-icon { font-size: 42px; color: #526985; line-height: 1; }
.stock-chart__spinner { width: 23px; height: 23px; border: 2px solid #223550; border-top-color: #79b9fa; border-radius: 50%; animation: chart-spin .8s linear infinite; }
.stock-chart__paging, .stock-chart__page-error { position: absolute; z-index: 1; top: 44px; left: 14px; display: flex; align-items: center; gap: 7px; padding: 6px 10px; border: 1px solid #2a3c55; border-radius: 4px; color: #a5b7cf; background: #122035ee; font-size: 11px; }
.stock-chart__paging .stock-chart__spinner { width: 12px; height: 12px; }
.stock-chart__page-error button { padding: 0; border: 0; color: #8fc6ff; background: transparent; cursor: pointer; font: inherit; }
@keyframes chart-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .stock-chart__spinner { animation: none; } }
</style>
