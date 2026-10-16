<script setup lang="ts">
import { computed } from 'vue'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import VChart from 'vue-echarts'
import type { Book, Instrument } from '../types'
import { compact, price } from '../format'
use([CanvasRenderer, LineChart, GridComponent, TooltipComponent])
const props = defineProps<{ book: Book | null; instrument: Instrument }>()
const option = computed(() => {
  function accumulate(levels: [number, number][]) {
    let sum = 0
    return levels.map(([px, size]) => [px, sum += size])
  }
  const bids = accumulate(props.book?.bids ?? []).reverse()
  const asks = accumulate(props.book?.asks ?? [])
  return {
    animation: false,
    grid: { left: 65, right: 35, top: 50, bottom: 45 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' }, backgroundColor: '#182232', borderColor: '#344252', textStyle: { color: '#dfe9f7' },
      formatter: (items: any[]) => items.filter(i => Array.isArray(i.value)).map(i => `${i.seriesName}<br/>${price(i.value[0], props.instrument.pricePrecision)} ${props.instrument.quote}<br/>累计 ${compact(i.value[1])} ${props.instrument.base}`).join('<br/>') },
    xAxis: { type: 'value', scale: true, splitLine: { show: false }, axisLine: { lineStyle: { color: '#263246' } }, axisLabel: { color: '#768598', fontSize: 10, formatter: (v: number) => price(v, props.instrument.pricePrecision) } },
    yAxis: { type: 'value', position: 'right', splitNumber: 5, splitLine: { lineStyle: { color: '#1a2331', type: 'dashed' } }, axisLabel: { color: '#768598', fontSize: 10, formatter: compact } },
    series: [{ name: '买盘', type: 'line', step: 'end', data: bids, showSymbol: false, lineStyle: { color: '#21c7a0', width: 2 }, areaStyle: { color: '#21c7a02c' } },
      { name: '卖盘', type: 'line', step: 'start', data: asks, showSymbol: false, lineStyle: { color: '#f45b6e', width: 2 }, areaStyle: { color: '#f45b6e2c' } }],
  }
})
</script>
<template><div class="depth-chart"><div class="depth-caption">累计挂单量 / {{ instrument.base }}<span>欧易 400 档深度 · 实时</span></div><VChart :option="option" autoresize aria-label="买卖累计深度图" /><div v-if="!book" class="depth-empty">等待盘口数据</div></div></template>
<style scoped>
.depth-chart{height:100%;position:relative}.depth-chart>.echarts{height:100%;width:100%}.depth-caption{position:absolute;z-index:1;top:17px;left:18px;right:18px;display:flex;justify-content:space-between;color:#7b8a9e;font-size:11px}.depth-caption span{color:#4d617a}.depth-empty{position:absolute;inset:0;display:grid;place-items:center;color:#75869e}
</style>
