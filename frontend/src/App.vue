<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ArrowDown, FullScreen, Minus, Plus, Refresh, Search, Star, StarFilled, Connection, TrendCharts } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import StockChart from './components/StockChart.vue'
import DepthChart from './components/DepthChart.vue'
import OrderBook from './components/OrderBook.vue'
import RecentTrades from './components/RecentTrades.vue'
import DemoAccount from './components/DemoAccount.vue'
const StrategyWorkbench = defineAsyncComponent(() => import('./components/StrategyWorkbench.vue'))
import { useOKX } from './composables/useOKX'
import { PERIODS, type CandlePeriod, type CandlesResponse, type ChartRange, type Instrument } from './types'
import { price, compact, percent, time, dateTime } from './format'

function stored(key: string, fallback: string) { try { return localStorage.getItem(key) ?? fallback } catch { return fallback } }
const savedId = stored('rebs.okx.instrument', 'BTC-USDT')
const selectedId = ref(/^[A-Z0-9]{1,24}-[A-Z0-9]{1,12}$/.test(savedId) ? savedId : 'BTC-USDT')
const savedPeriod = stored('rebs.okx.period', '15m')
const period = ref<CandlePeriod>(PERIODS.includes(savedPeriod as CandlePeriod) ? savedPeriod as CandlePeriod : '15m')
const favorites = ref<string[]>(readFavorites())
const search = ref('')
const favoriteOnly = ref(false)
const quote = ref('USDT')
const sort = ref('volume')
const chartMode = ref('candles')
const showMA = ref(true)
const showMACD = ref(true)
const chartRef = ref<InstanceType<typeof StockChart> | null>(null)
const chartPanel = ref<HTMLElement>()
const searchInput = ref<HTMLInputElement>()
const range = ref<ChartRange | null>(null)
const loading = ref(true)
const more = ref(true)
const chartError = ref('')
const manualRefresh = ref(0)
const lastClose = ref<number | null>(null)
const infoTab = ref('overview')
const workbenchOpen = ref(window.location.hash === '#strategy')
function syncPage() { workbenchOpen.value = window.location.hash === '#strategy' }
function showMarket(favoritesOnly = false) { favoriteOnly.value = favoritesOnly; workbenchOpen.value = false; if (window.location.hash === '#strategy') window.location.hash = 'market' }
function showStrategy() { workbenchOpen.value = true; window.location.hash = 'strategy'; window.scrollTo({top:0}) }
const { markets, marketError, marketLoading, marketUpdated, ticker, book, trades, liveBar, publicState, businessState,
  streamError, snapshotError, tradeSource, lastMessage, revision, frozen, now, stale, live, refreshMarkets, restart } = useOKX(selectedId, period)
const fallback = computed<Instrument>(() => ({ instId: selectedId.value, base: selectedId.value.split('-')[0]!, quote: selectedId.value.split('-')[1]!,
  tickSize: '0.1', lotSize: '0.00000001', minSize: '0.00001', pricePrecision: 1, volumePrecision: 8 }))
const instrument = computed(() => markets.value.find(m => m.instId === selectedId.value) ?? fallback.value)
const currentTicker = computed(() => ticker.value ?? markets.value.find(m => m.instId === selectedId.value) ?? null)
const isFavorite = computed(() => favorites.value.includes(selectedId.value))
const change = computed(() => currentTicker.value?.change24h ?? null)
const up = computed(() => (change.value ?? 0) >= 0)
const revisionKey = computed(() => revision.value + manualRefresh.value)
const names: Record<string,string> = { BTC:'Bitcoin', ETH:'Ethereum', SOL:'Solana', DOGE:'Dogecoin', XRP:'XRP', OKB:'OKB', USDT:'Tether', USDC:'USD Coin', ADA:'Cardano', AVAX:'Avalanche', TON:'Toncoin', LINK:'Chainlink' }
const coinName = computed(() => names[instrument.value.base] ?? instrument.value.base)
const quotes = computed(() => ['USDT','USDC','USD','BTC','ETH'].filter(q => q === 'USDT' || markets.value.some(m => m.quote === q)))
const filteredMarkets = computed(() => {
  const query = search.value.trim().toUpperCase().replace('/', '-')
  const filtered = markets.value.filter(m => (!query ? m.quote === quote.value : m.instId.includes(query)) && (!favoriteOnly.value || favorites.value.includes(m.instId)))
  return [...filtered].sort((a,b) => sort.value === 'change' ? (b.change24h ?? 0)-(a.change24h ?? 0) : b.quoteVolume24h-a.quoteVolume24h)
})
const savedMarkets = computed(() => favorites.value.map(id => markets.value.find(m=>m.instId===id)).filter(m => !!m))
const visibleTicker = (id: string) => id === selectedId.value && ticker.value ? ticker.value : markets.value.find(m=>m.instId===id)
const rangePosition = computed(() => {
  const t = currentTicker.value
  return t && t.high24h > t.low24h ? Math.max(0,Math.min(100,(t.last-t.low24h)/(t.high24h-t.low24h)*100)) : 50
})
const networkLabel = computed(() => live.value ? '实时连接正常' : stale.value ? '行情更新延迟' : publicState.value === 'reconnecting' || businessState.value === 'reconnecting' ? '正在重新连接' : '正在连接欧易')
const notice = computed(() => streamError.value || (publicState.value !== 'live' && snapshotError.value) || '')
const chartPeriodLabel = computed(() => ({'1s':'1 秒','1m':'1 分钟','3m':'3 分钟','5m':'5 分钟','15m':'15 分钟','30m':'30 分钟','1H':'1 小时','2H':'2 小时','4H':'4 小时','1D':'1 日','1W':'1 周','1M':'1 月'})[period.value])

function readFavorites(): string[] {
  try {
    const values: unknown = JSON.parse(stored('rebs.okx.favorites','null'))
    if (Array.isArray(values)) return values.filter((v): v is string => typeof v === 'string' && /^[A-Z0-9]{1,24}-[A-Z0-9]{1,12}$/.test(v)).slice(0,100)
  } catch { /* Keep local preferences optional. */ }
  return ['BTC-USDT','ETH-USDT','SOL-USDT','OKB-USDT','DOGE-USDT']
}
function persist(key: string, value: string) { try { localStorage.setItem(key,value) } catch { ElMessage.info('浏览器未允许保存，本次仍可使用。') } }
function toggleFavorite(id = selectedId.value) {
  favorites.value = favorites.value.includes(id) ? favorites.value.filter(v=>v!==id) : [...favorites.value,id].slice(-100)
  persist('rebs.okx.favorites',JSON.stringify(favorites.value))
}
function selectPair(id: string) { selectedId.value = id }
async function showDemo() { showMarket(); await nextTick(); document.getElementById('demo-account')?.scrollIntoView({behavior:'smooth',block:'start'}) }
function loaded(response: CandlesResponse) {
  if (response.instId !== selectedId.value || response.period !== period.value) return
  lastClose.value = response.bars.at(-1)?.close ?? null
  chartError.value = ''
}
function chartFailure(message: string) { chartError.value = message; loading.value = false }
function refresh() { manualRefresh.value++; restart(); void refreshMarkets() }
async function fullscreen() {
  try { if (document.fullscreenElement) await document.exitFullscreen(); else await chartPanel.value?.requestFullscreen() }
  catch { ElMessage.info('当前浏览器未允许全屏显示。') }
}
function shortcut(event: KeyboardEvent) {
  if (workbenchOpen.value) return
  if (event.key === '/' && !['INPUT','TEXTAREA','SELECT'].includes((event.target as HTMLElement)?.tagName)) { event.preventDefault(); searchInput.value?.focus() }
}
watch([selectedId,period], () => {
  range.value = null; loading.value = true; more.value = true; chartError.value = ''; lastClose.value = null
  persist('rebs.okx.instrument',selectedId.value); persist('rebs.okx.period',period.value)
})
onMounted(() => { window.addEventListener('keydown',shortcut); window.addEventListener('hashchange',syncPage) })
onBeforeUnmount(() => { window.removeEventListener('keydown',shortcut); window.removeEventListener('hashchange',syncPage) })
</script>

<template>
  <div class="exchange-shell">
    <header class="exchange-header">
      <a class="exchange-brand" href="/" aria-label="ReBS 数字资产看盘终端"><span class="brand-emblem"><i></i><i></i><i></i><i></i><i></i></span>ReBS<span class="brand-subtitle">TERMINAL</span></a>
      <nav class="header-nav" aria-label="工作台导航"><button :class="{active:!workbenchOpen && !favoriteOnly}" @click="showMarket()">现货行情</button><button :class="{active:!workbenchOpen && favoriteOnly}" @click="showMarket(true)">我的自选 <span>{{ favorites.length }}</span></button><button @click="showDemo">模拟交易</button><button :class="{active:workbenchOpen}" @click="showStrategy">策略工作台</button></nav>
      <div class="header-right"><span class="data-provider">行情来自 <strong>OKX 欧易</strong></span><button class="connection-pill" :class="{ online:live, warning:stale }" title="点击重新连接行情" @click="refresh"><span class="small-dot" :class="{live}"></span>{{ networkLabel }}</button><span class="local-badge">本地工作台</span></div>
    </header>

    <div v-show="!workbenchOpen" class="ticker-ribbon"><span class="ribbon-label"><el-icon><StarFilled/></el-icon>自选行情</span><button v-for="item in savedMarkets" :key="item.instId" :class="{ selected:item.instId===selectedId }" @click="selectPair(item.instId)"><strong>{{ item.base }}/{{ item.quote }}</strong><span>{{ price(visibleTicker(item.instId)?.last,item.pricePrecision) }}</span><span :class="(visibleTicker(item.instId)?.change24h ?? 0)>=0 ? 'up':'down'">{{ percent(visibleTicker(item.instId)?.change24h) }}</span></button><span v-if="!savedMarkets.length" class="ribbon-empty">点击币对旁的星标，添加自选</span></div>

    <StrategyWorkbench v-if="workbenchOpen" :instrument="instrument" @close="showMarket()" />

    <div v-show="!workbenchOpen" class="terminal-layout">
      <aside class="market-sidebar panel">
        <div class="sidebar-heading"><h2>{{ favoriteOnly?'我的自选':'币币行情' }}</h2><span>{{ filteredMarkets.length }} 个币对</span><button class="icon-button" title="刷新币对" aria-label="刷新币对列表" @click="refreshMarkets"><el-icon><Refresh/></el-icon></button></div>
        <label class="pair-search"><el-icon><Search/></el-icon><input ref="searchInput" v-model="search" placeholder="搜索币种 / 交易对" aria-label="搜索币种或交易对" /><kbd>/</kbd></label>
        <div class="quote-tabs" role="group" aria-label="计价币种"><button v-for="q in quotes" :key="q" :class="{active:quote===q}" @click="quote=q">{{ q }}</button></div>
        <div class="market-columns"><span>币对 / 成交量</span><select v-model="sort" aria-label="币对排序"><option value="volume">成交量排序 ↓</option><option value="change">涨幅排序 ↓</option></select></div>
        <div v-if="marketError" class="market-alert"><span>{{ marketError }}</span><button @click="refreshMarkets">重试</button></div>
        <div class="market-list"><div v-for="item in filteredMarkets" :key="item.instId" class="market-row" :class="{selected:item.instId===selectedId}"><button class="market-star" :aria-label="`${favorites.includes(item.instId)?'移出':'加入'}自选 ${item.instId}`" :class="{saved:favorites.includes(item.instId)}" @click="toggleFavorite(item.instId)"><el-icon><StarFilled v-if="favorites.includes(item.instId)"/><Star v-else/></el-icon></button><button class="market-pair" :aria-label="`查看 ${item.instId}`" @click="selectPair(item.instId)"><span class="pair-identity"><strong>{{ item.base }}<small>/{{ item.quote }}</small></strong><span>{{ compact(item.quoteVolume24h) }} {{ item.quote }}</span></span><span class="pair-price"><strong>{{ price(visibleTicker(item.instId)?.last,item.pricePrecision) }}</strong><span :class="(visibleTicker(item.instId)?.change24h??0)>=0?'up':'down'">{{ percent(visibleTicker(item.instId)?.change24h) }}</span></span></button></div><div v-if="marketLoading" class="list-empty">正在连接欧易市场…</div><div v-else-if="!filteredMarkets.length && !marketError" class="list-empty">{{ favoriteOnly?'暂无匹配的自选币对':'未找到匹配币对' }}</div></div>
        <div class="sidebar-footer"><span class="small-dot" :class="{live:!!marketUpdated}"></span>币对列表每 15 秒更新<span>{{ time(marketUpdated,false) }}</span></div>
      </aside>

      <main class="center-workspace">
        <section class="market-header panel"><div class="pair-heading"><div class="coin-icon" :class="instrument.base.toLowerCase()">{{ instrument.base==='BTC'?'₿':instrument.base.slice(0,1) }}</div><div><div class="pair-title"><h1>{{ instrument.base }}<span>/{{ instrument.quote }}</span></h1><span class="spot-badge">现货</span><button class="icon-button favorite" :class="{saved:isFavorite}" :aria-label="isFavorite?'移出当前自选':'加入当前自选'" @click="toggleFavorite()"><el-icon><StarFilled v-if="isFavorite"/><Star v-else/></el-icon></button></div><span class="pair-subtitle">{{ coinName }} <span>·</span> OKX</span></div></div><div class="last-price" :class="up?'up':'down'"><strong>{{ price(currentTicker?.last,instrument.pricePrecision) }}</strong><span>{{ percent(change) }} <small>24h</small></span></div><div class="header-stat"><span>24h 最高</span><strong>{{ price(currentTicker?.high24h,instrument.pricePrecision) }}</strong></div><div class="header-stat"><span>24h 最低</span><strong>{{ price(currentTicker?.low24h,instrument.pricePrecision) }}</strong></div><div class="header-stat volume-stat"><span>24h 成交额 ({{ instrument.quote }})</span><strong>{{ compact(currentTicker?.quoteVolume24h) }}</strong></div><button class="icon-button quote-refresh" aria-label="刷新当前行情" title="重新连接与刷新" @click="refresh"><el-icon><Refresh/></el-icon></button></section>

        <div v-if="notice || stale" class="connection-notice"><el-icon><Connection/></el-icon>{{ notice || '行情超过 30 秒未更新，请检查网络连接。' }}<button @click="refresh">重新连接</button></div>

        <section ref="chartPanel" class="terminal-chart panel">
          <div class="chart-tabbar"><div class="chart-tabs"><button :class="{active:chartMode==='candles'}" @click="chartMode='candles'">K 线图</button><button :class="{active:chartMode==='depth'}" @click="chartMode='depth'">深度图</button></div><div class="chart-platform"><el-icon><TrendCharts/></el-icon>专业图表<span>UTC+8</span></div></div>
          <div class="period-toolbar"><div class="period-buttons" role="group" aria-label="K线周期"><button v-for="p in PERIODS" :key="p" :class="{active:period===p}" :aria-pressed="period===p" @click="period=p;chartMode='candles'">{{ p }}</button></div><div class="chart-actions"><button class="icon-button" aria-label="缩小K线" title="缩小" @click="chartRef?.zoomOut()"><el-icon><Minus/></el-icon></button><button class="icon-button" aria-label="放大K线" title="放大" @click="chartRef?.zoomIn()"><el-icon><Plus/></el-icon></button><button class="icon-button" aria-label="全屏图表" title="全屏" @click="fullscreen"><el-icon><FullScreen/></el-icon></button></div></div>
          <div v-show="chartMode==='candles'" class="indicator-bar"><span>{{ instrument.base }}/{{ instrument.quote }} <i>·</i> {{ chartPeriodLabel }} <i>·</i> OKX</span><button :class="{active:showMA}" :aria-pressed="showMA" @click="showMA=!showMA"><i class="ma-dot"></i>MA</button><button :class="{active:showMACD}" :aria-pressed="showMACD" @click="showMACD=!showMACD"><i class="macd-dot"></i>MACD</button><span class="indicator-volume">VOL</span><span class="candle-state" :class="{forming:liveBar && !liveBar.confirmed}">{{ liveBar ? liveBar.confirmed?'K 线已收盘':'当前 K 线形成中':'历史 K 线' }}</span></div>
          <div class="crypto-chart-stage"><StockChart v-show="chartMode==='candles'" :key="`${selectedId}:${period}`" ref="chartRef" :instrument="instrument" :period="period" :live-bar="liveBar" :show-m-a="showMA" :show-m-a-c-d="showMACD" :refresh-key="revisionKey" @loaded="loaded" @range="range=$event.count?$event:null" @more="more=$event" @loading="loading=$event" @error="chartFailure"/><DepthChart v-if="chartMode==='depth'" :book="book" :instrument="instrument" /></div>
          <div class="chart-footer"><span><span class="small-dot" :class="{live:businessState==='live'}"></span>{{ range?`${range.count.toLocaleString()} 根 K 线`:'正在加载 K 线' }}<span v-if="range" class="range-dates">{{ dateTime(range.from) }} — {{ dateTime(range.to) }}</span></span><div><button class="text-button" @click="chartRef?.resetView()">回到最新</button><button :disabled="loading || !more || !range" class="text-button history-button" @click="chartRef?.loadMore()">{{ loading && range?'加载中…':more?'更早历史':'已到历史起点' }}<el-icon><ArrowDown/></el-icon></button></div></div>
          <div v-if="chartError && range" class="chart-inline-error">{{ chartError }}</div>
        </section>

        <section class="market-information panel"><div class="info-tabs"><button :class="{active:infoTab==='overview'}" @click="infoTab='overview'">市场概览</button><button :class="{active:infoTab==='instrument'}" @click="infoTab='instrument'">币对信息</button><span>24 小时滚动行情</span></div><div v-if="infoTab==='overview'" class="overview-content"><div class="overview-range"><span class="eyebrow">24H PRICE RANGE</span><div class="range-numbers"><strong>{{ price(currentTicker?.low24h,instrument.pricePrecision) }}</strong><span>现价 {{ price(currentTicker?.last,instrument.pricePrecision) }}</span><strong>{{ price(currentTicker?.high24h,instrument.pricePrecision) }}</strong></div><div class="price-range-track"><i :style="{left:rangePosition+'%'}"></i></div><div class="range-captions"><span>24h 最低</span><span>24h 最高</span></div></div><div class="overview-metric"><span>24h 涨跌额</span><strong :class="up?'up':'down'">{{ currentTicker?price(currentTicker.last-currentTicker.open24h,instrument.pricePrecision):'—' }}</strong><small>{{ instrument.quote }}</small></div><div class="overview-metric"><span>24h 成交量</span><strong>{{ compact(currentTicker?.volume24h) }}</strong><small>{{ instrument.base }}</small></div></div><div v-else class="instrument-details"><div><span>交易对</span><strong>{{ instrument.instId }}</strong></div><div><span>交易市场</span><strong>OKX 现货</strong></div><div><span>最小价格变动</span><strong>{{ instrument.tickSize }} {{ instrument.quote }}</strong></div><div><span>最小下单量</span><strong>{{ instrument.minSize }} {{ instrument.base }}</strong></div><div><span>数量步长</span><strong>{{ instrument.lotSize }} {{ instrument.base }}</strong></div><div><span>K 线时区</span><strong>UTC+8</strong></div></div></section>
        <DemoAccount :instrument="instrument" :last-price="currentTicker?.last"/>
        <div class="workspace-note"><span><kbd>滚轮</kbd> 缩放 <i>·</i><kbd>拖动</kbd> 查看历史 <i>·</i><kbd>/</kbd> 搜索币对</span><span>公开现货行情 · 绿涨红跌</span></div>
      </main>

      <aside class="right-workspace"><OrderBook :book="book" :instrument="instrument" :ticker="currentTicker" :connected="publicState==='live' && !stale"/><RecentTrades :trades="trades" :instrument="instrument" v-model:frozen="frozen" :source="tradeSource"/></aside>
    </div>
    <footer class="status-footer"><span><span class="small-dot" :class="{live}"></span>{{ networkLabel }}<span class="footer-divider"></span>盘口 {{ publicState==='live'?'实时':publicState==='reconnecting'?'重连中':'连接中' }}<span class="footer-divider"></span>成交 / K 线 {{ businessState==='live'?'实时':businessState==='reconnecting'?'重连中':'连接中' }}</span><span>最近推送 {{ time(lastMessage) }}<span class="footer-divider"></span>{{ time(now) }} UTC+8</span></footer>
  </div>
</template>
