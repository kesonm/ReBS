<script setup lang="ts">
import { computed } from 'vue'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import VChart from 'vue-echarts'
import type { Bar } from '../types'

use([CanvasRenderer, LineChart, GridComponent, TooltipComponent])
const props = defineProps<{ bars: Bar[] }>()
const option = computed(() => {
  const bars = props.bars.slice(-60)
  return {
    animation: false,
    grid: { left: 2, right: 3, top: 12, bottom: 6 },
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#111e30',
      borderColor: '#2a3d56',
      textStyle: { color: '#dce6f5', fontSize: 11 },
      valueFormatter: (value: number) => Number(value).toFixed(2),
    },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      show: false,
      data: bars.map(bar => new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' }).format(bar.timestamp)),
    },
    yAxis: { type: 'value', scale: true, show: false },
    series: [{
      type: 'line',
      data: bars.map(bar => bar.close),
      showSymbol: false,
      lineStyle: { width: 2, color: '#659cff' },
      itemStyle: { color: '#659cff' },
      areaStyle: {
        color: {
          type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [{ offset: 0, color: '#377cde45' }, { offset: 1, color: '#377cde00' }],
        },
      },
    }],
  }
})
</script>

<template>
  <VChart :option="option" autoresize class="price-overview" aria-label="最近60个交易日收盘走势" />
</template>

<style scoped>
.price-overview { height: 105px; width: 100%; }
</style>
