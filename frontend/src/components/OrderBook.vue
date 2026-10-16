<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { Book, Instrument, Ticker } from '../types'
import { price, compact } from '../format'
const props = defineProps<{ book: Book | null; instrument: Instrument; ticker: Ticker | null; connected: boolean }>()
const factor = ref(1)
const rows = ref(10)
const askLadder = ref<HTMLElement>()
watch(() => props.instrument.instId, () => { factor.value = 1 })
const step = computed(() => Number(props.instrument.tickSize) * factor.value)
const grouped = computed(() => {
  function group(levels: [number,number][], ask: boolean) {
    const byPrice = new Map<number,number>()
    for (const [px, size] of levels) {
      const group = Number(((ask ? Math.ceil(px / step.value - 1e-7) : Math.floor(px / step.value + 1e-7)) * step.value).toFixed(props.instrument.pricePrecision))
      byPrice.set(group, (byPrice.get(group) ?? 0) + size)
    }
    let cumulative = 0
    return [...byPrice].sort((a,b) => ask ? a[0]-b[0] : b[0]-a[0]).slice(0,rows.value).map(([px,size]) => ({ price:px, size, total:cumulative+=size }))
  }
  const bids = group(props.book?.bids ?? [], false)
  const asks = group(props.book?.asks ?? [], true)
  const maximum = Math.max(...bids.map(l=>l.total), ...asks.map(l=>l.total), 1e-12)
  return { bids, asks: asks.reverse(), maximum }
})
watch(() => grouped.value.asks.length, () => {
  if (askLadder.value) askLadder.value.scrollTop = askLadder.value.scrollHeight
}, { flush: 'post' })
const ratio = computed(() => {
  const bid = props.book?.bids.reduce((n,l)=>n+l[1],0) ?? 0
  const ask = props.book?.asks.reduce((n,l)=>n+l[1],0) ?? 0
  return bid+ask ? bid/(bid+ask)*100 : 50
})
const spread = computed(() => props.book?.asks[0] && props.book?.bids[0] ? props.book.asks[0][0]-props.book.bids[0][0] : null)
const spreadPercent = computed(() => spread.value !== null && props.ticker?.last ? spread.value / props.ticker.last *100 : null)
</script>
<template>
  <section class="orderbook panel">
    <div class="panel-heading"><h2>委托订单</h2><span class="tiny-tag">400 档深度</span><span class="small-dot" :class="{ live: connected }"></span></div>
    <div class="book-options"><span class="book-legend"><i class="buy-block"></i><i class="sell-block"></i>买卖盘</span><select v-model.number="rows" aria-label="盘口显示档数"><option :value="10">10 档</option><option :value="20">20 档</option><option :value="50">50 档</option></select><select v-model.number="factor" aria-label="盘口价格精度"><option v-for="n in [1,10,100,1000]" :key="n" :value="n">{{ price(Number(instrument.tickSize)*n, instrument.pricePrecision) }}</option></select></div>
    <div class="book-columns"><span>价格 ({{ instrument.quote }})</span><span>数量 ({{ instrument.base }})</span><span>累计</span></div>
    <div ref="askLadder" class="book-ladder asks" :style="{ maxHeight: rows === 10 ? '190px' : '270px' }">
      <div v-for="level in grouped.asks" :key="level.price" class="book-row sell"><i :style="{ width: `${level.total/grouped.maximum*100}%` }"></i><span>{{ price(level.price,instrument.pricePrecision) }}</span><span>{{ compact(level.size) }}</span><span>{{ compact(level.total) }}</span></div>
      <div v-if="!book" class="book-placeholder">等待委托订单</div>
    </div>
    <div class="book-mid"><strong :class="ticker && ticker.last >= ticker.open24h ? 'up' : 'down'">{{ price(ticker?.last,instrument.pricePrecision) }}</strong><span>价差 {{ price(spread,instrument.pricePrecision) }} <small>({{ spreadPercent === null ? '—' : spreadPercent.toFixed(4)+'%' }})</small></span></div>
    <div class="book-ladder bids" :style="{ maxHeight: rows === 10 ? '190px' : '270px' }"><div v-for="level in grouped.bids" :key="level.price" class="book-row buy"><i :style="{ width: `${level.total/grouped.maximum*100}%` }"></i><span>{{ price(level.price,instrument.pricePrecision) }}</span><span>{{ compact(level.size) }}</span><span>{{ compact(level.total) }}</span></div><div v-if="!book" class="book-placeholder">等待委托订单</div></div>
    <div class="book-balance"><span class="up">买 {{ book ? ratio.toFixed(1)+'%' : '—' }}</span><div><i :style="{width:(book ? ratio : 0)+'%'}"></i></div><span class="down">{{ book ? (100-ratio).toFixed(1)+'%' : '—' }} 卖</span></div>
  </section>
</template>
